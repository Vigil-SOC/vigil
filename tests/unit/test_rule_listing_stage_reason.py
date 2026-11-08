"""Listing rows carry the reason for each Vigil rule's latest stage move, so the
console can tell a new version going back to shadow for its week from Vigil
reverting a live rule. SIEM rows carry no reason."""

import os
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from sqlalchemy import text

from core.detections.inventory import catalog

NEW_VERSION = "new version"
LOUD = "a live day above 20 hits"


def _rows(rule_id, *newest_first):
    """(rule_id, stage, actor, proof, reason) rows, newest first, as STAGE_ROWS orders them."""
    return [
        (rule_id, stage, actor, proof, reason)
        for stage, actor, proof, reason in newest_first
    ]


@pytest.mark.parametrize(
    "newest, want",
    [
        # A live rule's new version starts its week in shadow.
        (("shadow", "system", {}, NEW_VERSION), NEW_VERSION),
        # Vigil reverted it for being loud.
        (("shadow", "system", {}, LOUD), LOUD),
        # A person reverted it and said why.
        (("shadow", "d.lee", {"hold": True}, "too loud"), "too loud"),
    ],
)
def test_the_reason_is_the_latest_moves(newest, want):
    out = catalog.stage_moves(
        _rows(7, newest, ("live", "system", {}, "7 quiet days"), ("shadow", "system", {}, NEW_VERSION))
    )
    assert out[7]["stage_reason"] == want


def test_a_row_with_no_reason_reads_empty():
    out = catalog.stage_moves(_rows(7, ("shadow", "system", {}, None)))
    assert out[7]["stage_reason"] == ""


def test_each_rule_has_its_own_reason():
    out = catalog.stage_moves(
        _rows(1, ("shadow", "system", {}, NEW_VERSION), ("live", "system", {}, ""))
        + _rows(2, ("shadow", "system", {}, LOUD), ("live", "system", {}, ""))
    )
    assert (out[1]["stage_reason"], out[2]["stage_reason"]) == (NEW_VERSION, LOUD)


def _copy(rule_id, source):
    return SimpleNamespace(
        id=rule_id, name=f"R{rule_id}", language="kql", source=source,
        status="active", techniques=[], checks=None,
    )


def test_vigil_rows_carry_the_reason_and_siem_rows_none():
    copies = [_copy(1, "overlay"), _copy(2, "elastic")]
    at = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)
    built = catalog._health_rows(
        {
            "rows": copies,
            "rule_of": {c.id: c.id for c in copies},
            "stages": {1: ("shadow", at)},
            "moves": catalog.stage_moves(
                _rows(1, ("shadow", "system", {}, NEW_VERSION), ("live", "system", {}, ""))
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
    assert rows[1]["stage_reason"] == NEW_VERSION
    assert "stage_reason" not in rows[2]


# On a real Postgres: the reason comes from the same row as the stage ----------
# Set VIGIL_TEST_PG_DSN to a throwaway database to run this (the unit suite has none).

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
def test_an_out_of_order_row_gives_the_reason_of_the_newest_in_time(db):
    """The newest row by id is not the newest by time; the reason must come from
    the row the listing's stage and GET /rules/{id}/stages call current."""
    from core.detections.inventory import readers, stages
    from core.storage.models import (
        DetectionRule,
        DetectionRuleStage,
        DetectionRuleVersion,
    )

    t0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
    with db.session_scope() as session:
        rule = DetectionRule(
            source=readers.OVERLAY_SOURCE, native_key="r", name="r",
            language="splunk", status="active", latest_version=2,
        )
        session.add(rule)
        session.flush()
        for v in (1, 2):
            session.add(DetectionRuleVersion(
                rule_id=rule.id, version=v, fingerprint=f"f{v}", content={}, checks={},
            ))
        for version, stage, reason, at in [
            (1, "shadow", NEW_VERSION, t0),
            (1, "live", "7 quiet days", t0 + timedelta(days=7)),
            (2, "shadow", NEW_VERSION, t0 + timedelta(days=9)),
            # Written last (highest id) but stamped before the new version's row.
            (1, "shadow", LOUD, t0 + timedelta(days=8)),
        ]:
            session.add(DetectionRuleStage(
                rule_id=rule.id, version=version, stage=stage, actor="system",
                proof={}, reason=reason, at=at,
            ))
        rid = rule.id

    with db.session_scope() as session:
        moves = catalog.stage_moves(session.execute(catalog.STAGE_ROWS))[rid]
    hist = stages.history(rid)
    assert moves["stage_reason"] == hist["stages"][0]["reason"] == NEW_VERSION
