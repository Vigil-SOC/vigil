"""Listing rows carry each Vigil rule's stage-move count, its last move's actor
and from-stage, and who holds it, as GET /rules/{id}/stages reads them; SIEM
rows carry none of these (#4050)."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from core.detections.inventory import catalog

FIELDS = {"stage_moves", "stage_from", "stage_actor", "stage_reason", "held", "held_by"}


def _rows(rule_id, *newest_first):
    """(rule_id, stage, actor, proof, reason) rows, newest first, as the query orders them."""
    return [(rule_id, stage, actor, proof, "") for stage, actor, proof in newest_first]


@pytest.mark.parametrize(
    "rows, want",
    [
        # One row: the copy entered shadow; nothing to move from.
        (
            [("shadow", "system", {})],
            {"stage_moves": 1, "stage_from": None, "stage_actor": "system", "stage_reason": "",
             "held": False, "held_by": None},
        ),
        # Vigil moved it back to shadow itself: the row turns amber.
        (
            [("shadow", "system", {}), ("live", "system", {}), ("shadow", "system", {})],
            {"stage_moves": 3, "stage_from": "live", "stage_actor": "system", "stage_reason": "",
             "held": False, "held_by": None},
        ),
        # A person reverted it: held by them.
        (
            [("shadow", "d.lee", {"hold": True}), ("live", "system", {})],
            {"stage_moves": 2, "stage_from": "live", "stage_actor": "d.lee", "stage_reason": "",
             "held": True, "held_by": "d.lee"},
        ),
        # A new version's row after the Revert does not lift the hold.
        (
            [("shadow", "system", {}), ("shadow", "d.lee", {"hold": True}),
             ("live", "system", {})],
            {"stage_moves": 3, "stage_from": "shadow", "stage_actor": "system", "stage_reason": "",
             "held": True, "held_by": "d.lee"},
        ),
        # Released: no longer held.
        (
            [("shadow", "a.kim", {"hold": False}), ("shadow", "d.lee", {"hold": True}),
             ("live", "system", {})],
            {"stage_moves": 3, "stage_from": "shadow", "stage_actor": "a.kim", "stage_reason": "",
             "held": False, "held_by": None},
        ),
    ],
)
def test_stage_moves_read_like_the_stages_endpoint(rows, want):
    assert catalog.stage_moves(_rows(7, *rows)) == {7: want}


def test_each_rule_is_summarised_on_its_own():
    out = catalog.stage_moves(
        _rows(1, ("live", "system", {}), ("shadow", "system", {}))
        + _rows(2, ("shadow", "d.lee", {"hold": True}), ("live", "system", {}))
    )
    assert (out[1]["stage_moves"], out[1]["held"]) == (2, False)
    assert (out[2]["held_by"], out[2]["stage_actor"]) == ("d.lee", "d.lee")


def _copy(rule_id, source):
    return SimpleNamespace(
        id=rule_id, name=f"R{rule_id}", language="kql", source=source,
        status="active", techniques=[], checks=None,
    )


def test_vigil_rows_carry_the_fields_and_siem_rows_none():
    copies = [_copy(1, "overlay"), _copy(2, "elastic")]
    at = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)
    built = catalog._health_rows(
        {
            "rows": copies,
            "rule_of": {c.id: c.id for c in copies},
            "stages": {1: ("shadow", at)},
            "moves": catalog.stage_moves(
                _rows(1, ("shadow", "d.lee", {"hold": True}), ("live", "system", {}))
            ),
        },
        {},
        today=date(2026, 11, 8),
    )
    rows = {
        r["rule_id"]: r
        for state in built["by_state"].values()
        for _tool, _hay, r in state
    }
    assert {k: rows[1][k] for k in FIELDS} == {
        "stage_moves": 2, "stage_from": "live", "stage_actor": "d.lee", "stage_reason": "",
        "held": True, "held_by": "d.lee",
    }
    assert not FIELDS & set(rows[2])


# On a real Postgres: one ordering everywhere ---------------------------------
# Set VIGIL_TEST_PG_DSN to a throwaway database to run these, e.g.
# postgresql://postgres:t@127.0.0.1:55453/t (the unit suite has none).

import os  # noqa: E402
from datetime import timedelta  # noqa: E402
from urllib.parse import urlparse  # noqa: E402

from sqlalchemy import select, text  # noqa: E402

DSN = os.environ.get("VIGIL_TEST_PG_DSN")
needs_pg = pytest.mark.skipif(not DSN, reason="VIGIL_TEST_PG_DSN not set")


@pytest.fixture
def db(monkeypatch):
    """The detection tables, empty, on the throwaway Postgres."""
    url = urlparse(DSN)
    monkeypatch.setenv("POSTGRES_HOST", url.hostname)
    monkeypatch.setenv("POSTGRES_PORT", str(url.port))
    monkeypatch.setenv("POSTGRES_DB", url.path.lstrip("/"))
    monkeypatch.setenv("POSTGRES_USER", url.username)
    monkeypatch.setenv("POSTGRES_PASSWORD", url.password)
    monkeypatch.setenv("POSTGRES_SSL_MODE", "disable")
    from core.storage import connection

    connection.DatabaseManager._instance = None
    monkeypatch.setattr(connection, "_db_manager", None)
    manager = connection.get_db_manager()
    manager.initialize(force=True)
    manager.create_tables()
    with manager.session_scope() as session:
        session.execute(text("TRUNCATE detection_rules RESTART IDENTITY CASCADE"))
    yield manager
    manager._engine.dispose()
    connection.DatabaseManager._instance = None


@needs_pg
def test_an_out_of_order_row_reads_the_same_in_the_listing_stages_and_rollout(db):
    """The newest row by id is not the newest by time. Stage, from-stage, actor and
    hold must all come from the same row order in every reader."""
    from core.detections.inventory import readers, rollout, stages
    from core.storage.models import (
        DetectionRule,
        DetectionRuleStage,
        DetectionRuleVersion,
    )

    t0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
    with db.session_scope() as session:
        rule = DetectionRule(
            source=readers.OVERLAY_SOURCE, native_key="r", name="r",
            language="splunk", status="active", latest_version=1,
        )
        session.add(rule)
        session.flush()
        session.add(DetectionRuleVersion(
            rule_id=rule.id, version=1, fingerprint="f", content={}, checks={},
        ))
        for stage, actor, proof, at in [
            ("shadow", "system", {}, t0),
            ("live", "system", {}, t0 + timedelta(days=8)),
            # Written last (highest id) but stamped earlier than the live row.
            ("shadow", "d.lee", {"hold": True}, t0 + timedelta(days=7)),
        ]:
            session.add(DetectionRuleStage(
                rule_id=rule.id, version=1, stage=stage, actor=actor,
                proof=proof, reason="", at=at,
            ))
        rid = rule.id

    with db.session_scope() as session:
        current = {r: s for r, s, _at in session.execute(select(catalog.CURRENT_STAGE))}
        moves = catalog.stage_moves(session.execute(catalog.STAGE_ROWS))[rid]
    hist = stages.history(rid)
    (copy,) = [c for c in rollout._copies() if c.rule_id == rid]

    # The newest by time is the live row; the earlier-stamped hold row precedes it.
    assert current[rid] == hist["stages"][0]["to_stage"] == copy.stage == "live"
    assert moves["stage_from"] == hist["stages"][0]["from_stage"] == "shadow"
    assert moves["stage_actor"] == hist["stages"][0]["actor"] == "system"
    assert moves["stage_moves"] == len(hist["stages"]) == 3
    assert moves["held"] is hist["held"] is copy.held is True
    assert moves["held_by"] == "d.lee"
