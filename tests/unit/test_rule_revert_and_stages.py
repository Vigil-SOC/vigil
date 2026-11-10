"""A person can read a rule's stage history, Revert it (live -> shadow) and
release it; a person's Revert holds the rule in shadow against the rollout
sweep; Vigil's own Revert reaches the bell (#3996)."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core.detections.inventory import catalog, health_inbox, rollout, stages
from core.detections.inventory.rollout_limits import DEFAULTS
from services.api.routers import detection_rules as routes

NOW = datetime(2026, 11, 7, 9, 30, tzinfo=timezone.utc)
DEFAULT_SHADOW_DAYS = DEFAULTS.shadow_days
ANA = SimpleNamespace(username="ana", user_id="u-ana")


def _stage(sid, stage, actor="system", reason="", hold=None, days_ago=0):
    return SimpleNamespace(
        id=sid,
        rule_id=7,
        version=2,
        stage=stage,
        actor=actor,
        reason=reason,
        proof={} if hold is None else {"hold": hold},
        at=NOW - timedelta(days=days_ago),
    )


class _Session:
    def __init__(self, rows):
        self.rows, self.added = rows, []

    def get(self, model, rule_id):
        return SimpleNamespace(id=rule_id, latest_version=2) if rule_id == 7 else None

    def scalars(self, _query):
        return SimpleNamespace(all=lambda: list(self.rows))

    def add(self, row):
        self.added.append(row)


@pytest.fixture
def db(monkeypatch):
    """Rule 7 with ``db.rows`` as its stage rows (newest first); records writes."""
    box = SimpleNamespace(rows=[], audits=[], session=None)

    @contextmanager
    def scope():
        box.session = _Session(box.rows)
        yield box.session

    monkeypatch.setattr(
        stages, "get_db_manager", lambda: SimpleNamespace(session_scope=scope)
    )
    monkeypatch.setattr(
        stages,
        "_state",
        lambda s, rid: (
            (s.get(None, rid), box.rows[0] if box.rows else None)
            if rid == 7
            else (None, None)
        ),
    )

    def held(session, rule_id):
        # Rows the session added count too, as autoflush would make them.
        pending = list(reversed(getattr(session, "added", [])))
        keyed = [r for r in pending + box.rows if "hold" in (r.proof or {})]
        return bool(keyed) and keyed[0].proof["hold"] is True

    monkeypatch.setattr(stages, "_held", held)

    class _Config:
        def __init__(self, user_id):
            self.user_id = user_id

        def _create_audit_log(self, session, **kw):
            box.audits.append({"user_id": self.user_id, **kw})

    monkeypatch.setattr(stages, "ConfigService", _Config)
    monkeypatch.setattr(
        "core.agents.attribution.principal_id_for_username", lambda name: f"p-{name}"
    )
    return box


# --- the stages read --------------------------------------------------------


def test_stages_are_the_rows_newest_first_with_from_and_to(db):
    db.rows = [
        _stage(3, "shadow", "ana", "too loud", hold=True),
        _stage(2, "live", "system", "7 days"),
        _stage(1, "shadow", "system", "new"),
    ]
    out = routes.get_rule_stages(7)["stages"]
    assert [(r["from_stage"], r["to_stage"], r["actor"]) for r in out] == [
        ("live", "shadow", "ana"),
        ("shadow", "live", "system"),
        (None, "shadow", "system"),
    ]
    assert out[0]["reason"] == "too loud" and out[0]["version"] == 2 and out[0]["at"]


@pytest.mark.parametrize(
    "rows, held",
    [
        ([], False),
        ([_stage(2, "shadow", "ana", "too loud", hold=True)], True),
        # A new version's system row after the Revert doesn't lift the hold.
        (
            [
                _stage(3, "shadow", "system", "v3"),
                _stage(2, "shadow", "ana", "x", hold=True),
            ],
            True,
        ),
        (
            [
                _stage(3, "shadow", "ana", "ok", hold=False),
                _stage(2, "shadow", "ana", "x", hold=True),
            ],
            False,
        ),
    ],
)
def test_stages_say_whether_a_person_holds_the_rule(db, rows, held):
    db.rows = rows
    assert routes.get_rule_stages(7)["held"] is held


def test_stages_of_an_unknown_rule_is_404(db):
    with pytest.raises(HTTPException) as e:
        routes.get_rule_stages(99)
    assert e.value.status_code == 404


# --- revert and release -----------------------------------------------------


def _body(reason="noisy"):
    return routes.StageChangeRequest(reason=reason)


def test_revert_writes_a_held_shadow_row_by_the_person_and_an_audit_row(db):
    db.rows = [_stage(2, "live")]
    out = routes.revert_rule(7, _body("noisy"), ANA)
    assert out["stage"] == "shadow" and out["hold"] is True
    (row,) = db.session.added
    assert (row.rule_id, row.version, row.stage, row.actor, row.reason) == (
        7,
        2,
        "shadow",
        "ana",
        "noisy",
    )
    assert row.proof == {"hold": True}
    (audit,) = db.audits
    assert audit["user_id"] == "p-ana"
    assert audit["config_key"] == "detection_rule:7"
    assert audit["change_reason"] == "noisy"
    assert (
        audit["old_value"]["stage"] == "live"
        and audit["new_value"]["stage"] == "shadow"
    )


@pytest.mark.parametrize("stage", ["shadow", "off"])
def test_reverting_a_rule_that_is_not_live_is_409_and_writes_nothing(db, stage):
    db.rows = [_stage(2, stage)]
    with pytest.raises(HTTPException) as e:
        routes.revert_rule(7, _body(), ANA)
    assert e.value.status_code == 409 and "not live" in e.value.detail
    assert db.session.added == [] and db.audits == []


def test_reverting_an_unknown_rule_is_404(db):
    with pytest.raises(HTTPException) as e:
        routes.revert_rule(99, _body(), ANA)
    assert e.value.status_code == 404


def test_a_reason_is_required():
    with pytest.raises(ValueError):
        routes.StageChangeRequest(reason="")
    # Whitespace only is no reason either: a 422 like an empty one.
    with pytest.raises(ValueError):
        routes.StageChangeRequest(reason="   \n\t")
    with pytest.raises(ValueError):
        routes.StageChangeRequest()


def test_revert_release_and_stages_routes_are_gated_as_documented():
    def gates(path, method):
        route = next(
            r for r in routes.router.routes if r.path == path and method in r.methods
        )
        return {d.dependency.permission for d in route.dependencies}

    assert gates("/rules/{rule_id}/revert", "POST") == {
        "detections.read",
        "detections.write",
    }
    assert gates("/rules/{rule_id}/release", "POST") == {
        "detections.read",
        "detections.write",
    }
    assert gates("/rules/{rule_id}/stages", "GET") == {"detections.read"}


def test_release_lifts_the_hold_with_a_row_and_an_audit_row(db):
    db.rows = [_stage(3, "shadow", "ana", hold=True)]
    out = routes.release_rule(7, _body("fixed the query"), ANA)
    assert out["hold"] is False
    (row,) = db.session.added
    assert (row.stage, row.actor, row.proof) == ("shadow", "ana", {"hold": False})
    assert db.audits[0]["new_value"]["event"] == "release"


@pytest.mark.parametrize(
    "row",
    [_stage(2, "live"), _stage(2, "shadow"), _stage(3, "shadow", "ana", hold=False)],
)
def test_releasing_a_rule_nobody_holds_is_409(db, row):
    db.rows = [row]
    with pytest.raises(HTTPException) as e:
        routes.release_rule(7, _body(), ANA)
    assert e.value.status_code == 409


def test_a_new_versions_row_does_not_lift_the_hold_only_a_release_does(db):
    # newest first: v3's system row after the person's hold row
    db.rows = [
        _stage(4, "shadow", "system", "new version"),
        _stage(3, "shadow", "ana", hold=True),
        _stage(2, "live"),
    ]
    out = routes.release_rule(7, _body("ok"), ANA)  # still held: release is allowed
    assert out["hold"] is False
    db.rows = [_stage(5, "shadow", "ana", hold=False)] + db.rows
    with pytest.raises(HTTPException) as e:
        routes.release_rule(7, _body(), ANA)
    assert e.value.status_code == 409


# --- the hold against the sweep ---------------------------------------------

HEALTHY_WEEK = {
    "runs": {
        "failures": 0,
        "days": {
            (NOW - timedelta(days=i)).date().isoformat(): {"pass": 5, "fail": 0}
            for i in range(DEFAULT_SHADOW_DAYS)
        },
    },
    "sees": {"result": "pass"},
    # Shown to fire: its save-time run in its SIEM caught the attack (#4212).
    "replay": {"result": "pass", "source": "siem"},
    # Promotion also needs the version's lint pass (#4011).
    "lint": {"result": "pass", "findings": []},
}


def _copy(held):
    return rollout.Copy(
        rule_id=7,
        version=2,
        name="r",
        language="elastic",
        stage="shadow",
        stage_at=NOW - timedelta(days=DEFAULT_SHADOW_DAYS + 1),
        checks=HEALTHY_WEEK,
        rule={},
        held=held,
    )


def test_a_held_rule_is_not_promoted_and_a_released_one_is():
    assert rollout._due(None, _copy(held=False), NOW, 0)[0] == "live"
    assert rollout._due(None, _copy(held=True), NOW, 0) is None


def test_the_sweep_writes_no_live_row_for_a_held_rule(monkeypatch):
    inserted = []

    class _Db:
        @contextmanager
        def session_scope(self):
            yield SimpleNamespace(
                scalar=lambda q: 0,
                execute=lambda q, rows=None: inserted.extend(rows or []),
            )

    monkeypatch.setattr(rollout, "get_db_manager", lambda: _Db())
    monkeypatch.setattr(rollout, "_stopped", lambda: False)
    monkeypatch.setattr(rollout, "_asks_first", lambda: False)  # Act
    monkeypatch.setattr(rollout, "handed_off", lambda ids: set())
    monkeypatch.setattr(rollout, "_deploy_targets", lambda: {})
    monkeypatch.setattr(rollout, "_deploy", lambda *a: None)
    monkeypatch.setattr(rollout, "_noise", lambda s, c: (0, 0))

    for held, moves in ((True, 0), (False, 1)):
        inserted.clear()
        monkeypatch.setattr(rollout, "_copies", lambda held=held: [_copy(held)])
        report = rollout.RolloutReport()
        rollout.sweep(NOW, report)
        assert len(report.moves) == moves and len(inserted) == moves


# --- Vigil's own Revert reaches the bell ------------------------------------


def test_vigils_revert_is_a_bell_entry_for_everyone(monkeypatch):
    revert = {
        "change_id": -9,
        "rule_id": 7,
        "name": "Rule 7",
        "tool": "overlay",
        "status": "active",
        "check": "revert",
        "old": "live",
        "new": "shadow",
        "version": 2,
        "reason": "Sees: lab: data stopped",
        "since": "2026-11-07T09:00:00+00:00",
    }
    monkeypatch.setattr(catalog, "open_flags", lambda: [])
    monkeypatch.setattr(catalog, "open_vigil_moves", lambda: [revert])
    reads = {}
    monkeypatch.setattr(health_inbox, "_read_ids", lambda u: reads.get(u, set()))

    def update(user_id, fn):
        reads[user_id] = fn(reads.get(user_id, set()))
        return reads[user_id]

    monkeypatch.setattr(health_inbox, "_update_read", update)
    for person in ("ana", "ben"):
        box = health_inbox.inbox(person)
        assert [e["check"] for e in box["entries"]] == ["revert"] and box["unread"] == 1
    health_inbox.mark_read("ana", [-9])
    assert health_inbox.inbox("ana")["unread"] == 0
    assert health_inbox.inbox("ben")["unread"] == 1


def test_releasing_a_held_rule_that_is_now_off_leaves_it_off(db):
    """A release lifts the hold only; it never moves an off rule to shadow."""
    db.rows = [
        _stage(4, "off", "system", "rule disabled in the SIEM"),
        _stage(3, "shadow", "ana", "too loud", hold=True),
    ]
    out = routes.release_rule(7, _body("ok again"), ANA)
    (row,) = db.session.added
    assert (row.stage, row.proof) == ("off", {"hold": False})
    assert out["stage"] == "off" and out["hold"] is False


def test_the_audit_old_value_is_the_hold_before_the_change(db):
    db.rows = [_stage(2, "live")]
    routes.revert_rule(7, _body("noisy"), ANA)
    assert db.audits[-1]["old_value"]["hold"] is False
    assert db.audits[-1]["new_value"]["hold"] is True

    db.rows = [_stage(3, "shadow", "ana", hold=True)]
    routes.release_rule(7, _body("fixed"), ANA)
    assert db.audits[-1]["old_value"]["hold"] is True
    assert db.audits[-1]["new_value"]["hold"] is False
