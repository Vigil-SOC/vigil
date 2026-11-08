"""Every stage move is audited in one shape: a person's Revert or release is the
same ``detections.rule_staged`` trail entry Vigil's own moves write, with the
actor, the from and to stages, the reason and the hold, so one filter finds all.
"""

import os
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from core.audit import trail, words
from core.detections.inventory import stages, sync

STAGED = "detections.rule_staged"


def _is_move_of(entry, rule_id):
    """The one filter: the action and the rule it moved."""
    return entry[trail.ACTION] == STAGED and entry[trail.OBJECT] == str(rule_id)


@pytest.fixture
def trail_box(monkeypatch):
    """The trail in use; every append recorded, in order."""
    box = SimpleNamespace(entries=[], gated=[])

    def append(session, payload, gated=True, **_kw):
        box.entries.append(payload)
        box.gated.append(gated)

    monkeypatch.setattr(trail, "in_use", lambda session: True)
    monkeypatch.setattr(trail, "append", append)
    return box


@pytest.fixture
def rule7(monkeypatch, trail_box):
    """Rule 7, live, with no hold; the person's write and its audit recorded."""
    box = SimpleNamespace(
        rows=[SimpleNamespace(stage="live", proof={})], added=[], configs=[]
    )

    class _Session:
        def add(self, row):
            box.added.append(row)
            box.rows.insert(0, row)

    @contextmanager
    def scope():
        yield _Session()

    monkeypatch.setattr(
        stages, "get_db_manager", lambda: SimpleNamespace(session_scope=scope)
    )
    monkeypatch.setattr(
        stages,
        "_state",
        lambda s, rid: (SimpleNamespace(id=7, latest_version=2), box.rows[0]),
    )
    monkeypatch.setattr(
        stages,
        "_held",
        lambda s, rid: next(
            (r.proof["hold"] is True for r in box.rows if "hold" in (r.proof or {})),
            False,
        ),
    )

    class _Config:
        def __init__(self, user_id):
            pass

        def _create_audit_log(self, session, **kw):
            box.configs.append(kw)

    monkeypatch.setattr(stages, "ConfigService", _Config)
    return box


def test_a_persons_revert_is_a_rule_staged_entry(rule7, trail_box):
    stages.revert(7, ("ana", "p-ana"), "noisy")
    (entry,) = trail_box.entries
    assert entry[trail.ACTION] == STAGED and entry[trail.OBJECT] == "7"
    assert entry[trail.ACTOR] == "p-ana" and entry["username"] == "ana"
    assert (entry["from_stage"], entry["to_stage"]) == ("live", "shadow")
    assert entry["reason"] == "noisy" and entry["version"] == 2
    assert entry["hold"] is True and entry["held_before"] is False
    assert entry["event"] == "revert"
    # One entry per move: no second config.change for the same Revert.
    assert rule7.configs == []
    # A person's write is gated like any other person's audit entry.
    assert trail_box.gated == [True]


def test_a_persons_release_is_a_rule_staged_entry(rule7, trail_box):
    stages.revert(7, ("ana", "p-ana"), "noisy")
    stages.release(7, ("ben", "p-ben"), "fixed")
    entry = trail_box.entries[-1]
    assert entry[trail.ACTION] == STAGED and entry[trail.ACTOR] == "p-ben"
    assert (entry["from_stage"], entry["to_stage"]) == ("shadow", "shadow")
    assert entry["hold"] is False and entry["held_before"] is True
    assert entry["reason"] == "fixed" and entry["event"] == "release"


def test_one_filter_finds_a_revert_a_release_and_a_system_move(rule7, trail_box):
    stages.revert(7, ("ana", "p-ana"), "noisy")
    stages.release(7, ("ben", "p-ben"), "fixed")
    sync._insert_stages(
        SimpleNamespace(execute=lambda *a, **k: None),
        [sync._stage_row(7, 2, "live", "7 days in shadow")],
        {7: "shadow"},
    )
    found = [e for e in trail_box.entries if _is_move_of(e, 7)]
    assert [
        (e[trail.ACTOR], e["from_stage"], e["to_stage"], e["hold"]) for e in found
    ] == [
        ("p-ana", "live", "shadow", True),
        ("p-ben", "shadow", "shadow", False),
        ("system", "shadow", "live", None),
    ]
    assert all(e["reason"] for e in found)


def test_where_the_trail_is_not_in_use_the_config_log_keeps_the_move(
    rule7, monkeypatch
):
    monkeypatch.setattr(trail, "in_use", lambda session: False)
    stages.revert(7, ("ana", "p-ana"), "noisy")
    (row,) = rule7.configs
    assert row["config_key"] == "detection_rule:7"
    assert row["new_value"]["event"] == "revert"


@pytest.mark.parametrize(
    "payload, said",
    [
        (
            {
                "rule_id": 7,
                "version": 2,
                "from_stage": "live",
                "to_stage": "shadow",
                "hold": True,
            },
            "Moved rule 7 v2 from live to shadow and held it there",
        ),
        (
            {
                "rule_id": 7,
                "version": 2,
                "from_stage": "shadow",
                "to_stage": "shadow",
                "hold": False,
            },
            "Released the hold on rule 7 v2 in shadow",
        ),
        (
            {
                "rule_id": 7,
                "version": 2,
                "from_stage": "shadow",
                "to_stage": "live",
                "hold": None,
            },
            "Moved rule 7 v2 from shadow to live",
        ),
        # An entry written before hold was recorded reads as it did.
        (
            {"rule_id": 7, "version": 2, "from_stage": None, "to_stage": "shadow"},
            "Put rule 7 v2 in shadow",
        ),
    ],
    ids=["revert", "release", "system", "old-entry"],
)
def test_the_trail_says_what_each_move_did(payload, said):
    assert words._staged(payload) == said


# --- real Postgres: the audit screen's own filter ---------------------------

DSN = os.environ.get("VIGIL_TEST_PG_DSN")


@pytest.mark.skipif(not DSN, reason="VIGIL_TEST_PG_DSN not set")
def test_the_trail_filter_finds_every_move_on_real_postgres(monkeypatch):
    """Revert, release and a sweep move on a throwaway Postgres; one TrailQuery
    (area detections, object the rule) returns all three, each with its actor."""
    from sqlalchemy import text

    from core.storage import connection

    url = urlparse(DSN)
    for key, value in {
        "POSTGRES_HOST": url.hostname,
        "POSTGRES_PORT": str(url.port),
        "POSTGRES_DB": url.path.lstrip("/"),
        "POSTGRES_USER": url.username,
        "POSTGRES_PASSWORD": url.password,
        "POSTGRES_SSL_MODE": "disable",
    }.items():
        monkeypatch.setenv(key, value)
    connection.DatabaseManager._instance = None
    monkeypatch.setattr(connection, "_db_manager", None)
    manager = connection.get_db_manager()
    manager.initialize(force=True)
    with manager.session_scope() as session:
        if not trail.in_use(session):
            pytest.skip("this database has no audit trail (run core.upgrade on it)")
        # Rule ids restart when another test truncates: count only this run's entries.
        start = session.execute(text("SELECT clock_timestamp()")).scalar()
        rule_id = session.execute(
            text(
                "INSERT INTO detection_rules (source, native_key, name, language, status,"
                " latest_version) VALUES ('repo:overlay', 'audit-probe-' ||"
                " md5(random()::text), 'Audit probe', 'elastic', 'active', 1) RETURNING id"
            )
        ).scalar()
        session.execute(
            text(
                "INSERT INTO detection_rule_stages (rule_id, version, stage, actor, reason, proof)"
                " VALUES (:r, 1, 'live', 'system', 'seed', '{}')"
            ),
            {"r": rule_id},
        )
    stages.revert(rule_id, ("ana", "p-ana"), "noisy")
    stages.release(rule_id, ("ben", "p-ben"), "fixed")
    with manager.session_scope() as session:
        sync._insert_stages(
            session,
            [sync._stage_row(rule_id, 1, "live", "7 days")],
            {rule_id: "shadow"},
        )
    with manager.session_scope() as session:
        found = trail.read(
            session,
            trail.TrailQuery(area="detections", object_ref=str(rule_id), since=start),
        )
    moves = sorted(
        (
            e
            for e in found
            if e.get(trail.ACTION, e.get("payload", {}).get(trail.ACTION)) == STAGED
        ),
        key=lambda e: e.get("seq", 0),
    )
    payloads = [e.get("payload", e) for e in moves]
    assert [
        (p[trail.ACTOR], p["from_stage"], p["to_stage"], p["hold"]) for p in payloads
    ] == [
        ("p-ana", "live", "shadow", True),
        ("p-ben", "shadow", "shadow", False),
        ("system", "shadow", "live", None),
    ]
