"""Deploying the live rules only Vigil runs: the deploy-PR run a promotion to
live queues, for each rule whose format has a deploy target and none queued yet."""

from contextlib import contextmanager
from datetime import datetime, timezone
from itertools import count

import pytest

from core.detections.inventory import rollout

TARGET = {
    "git_url": "https://example.invalid/acme/detections.git",
    "subdirectory": "rules",
}


def _copy(rule_id, language="elastic", stage="live", toml=""):
    return rollout.Copy(
        rule_id=rule_id,
        version=1,
        name=f"rule {rule_id}",
        language=language,
        stage=stage,
        stage_at=datetime.now(timezone.utc),
        checks={},
        rule={},
        key=f"key-{rule_id}",
        toml=toml,
        path=f"{rule_id}.toml",
    )


class _Session:
    def __init__(self):
        self.added = []
        self._ids = count(1)

    def add(self, row):
        self.added.append(row)

    def flush(self):
        for row in self.added:
            if getattr(row, "id", None) is None:
                row.id = next(self._ids)


@pytest.fixture
def estate(monkeypatch):
    session = _Session()

    class _Db:
        @contextmanager
        def session_scope(self):
            yield session

    state = {
        "copies": [
            _copy(1),
            _copy(2),
            _copy(3, language="splunk"),
            _copy(4, stage="shadow"),
            _copy(5),
        ],
        "gone": {5},
        "targets": {"elastic": TARGET},
        "queued": {2},
    }
    monkeypatch.setattr(rollout, "get_db_manager", lambda: _Db())
    monkeypatch.setattr(rollout, "_copies", lambda: state["copies"])
    monkeypatch.setattr(rollout, "handed_off", lambda ids: state["gone"] & set(ids))
    monkeypatch.setattr(rollout, "_deploy_targets", lambda: state["targets"])
    monkeypatch.setattr(
        rollout, "_deploys_queued", lambda _s, ids: state["queued"] & set(ids)
    )
    monkeypatch.setattr(rollout.exceptions, "listed", lambda _s, _rid: [])
    return state, session


def test_overlay_only_lists_live_rules_no_siem_runs(estate):
    rules = {r["id"]: r for r in rollout.overlay_only()}
    # shadow (4) and handed off (5) are not listed
    assert sorted(rules) == [1, 2, 3]
    assert rules[1]["deploy_target"] and not rules[1]["deploy_queued"]
    assert rules[2]["deploy_queued"]
    assert not rules[3]["deploy_target"]


def test_deploy_all_queues_only_what_can_go(estate):
    _, session = estate
    out = rollout.deploy_overlay_only()
    assert [q["rule_id"] for q in out["queued"]] == [1]
    assert {s["rule_id"]: s["reason"] for s in out["skipped"]} == {
        2: "a deploy pull request is already queued",
        3: "no splunk deploy target is set",
    }
    (trigger,) = session.added
    assert trigger.payload["workflow_id"] == rollout.DEPLOY_WORKFLOW
    assert rollout._deploy_marker(1) in trigger.payload["hypothesis"]


def test_deploy_named_rules_says_why_others_are_skipped(estate):
    out = rollout.deploy_overlay_only([1, 4, 99])
    assert [q["rule_id"] for q in out["queued"]] == [1]
    assert {s["rule_id"] for s in out["skipped"]} == {4, 99}


def test_a_splunk_rule_needs_its_overlay_file(estate, monkeypatch):
    state, session = estate
    state["targets"] = {"splunk": TARGET}
    monkeypatch.setattr(rollout, "_read_splunk_files", lambda copies: None)
    out = rollout.deploy_overlay_only([3])
    assert out["queued"] == []
    assert out["skipped"] == [
        {"rule_id": 3, "reason": "its overlay file could not be read"}
    ]
    assert session.added == []


@pytest.mark.parametrize(
    "trigger_state, run_status, blocks",
    [
        ("queued", None, True),
        ("launched", "executing", True),
        ("launched", "waiting_approval", True),
        ("launched", "failed", False),
        ("launched", "completed", False),
        ("merged", "executing", True),
        ("expired", None, False),
        ("not_offered", None, False),
    ],
)
def test_only_a_deploy_in_flight_blocks_another(trigger_state, run_status, blocks):
    assert rollout._deploy_in_flight(trigger_state, run_status) is blocks


def test_a_failed_deploy_run_can_be_queued_again(monkeypatch):
    brief = f"... {rollout._deploy_marker(1)} ..."

    class _Rows:
        def all(self):
            return [(brief, "launched", "failed"), (brief, "expired", None)]

    class _S:
        def execute(self, _stmt):
            return _Rows()

    assert rollout._deploys_queued(_S(), [1]) == set()


def test_no_target_wins_over_an_old_queued_run(estate):
    state, session = estate
    state["targets"] = {}
    out = rollout.deploy_overlay_only([2])
    assert out["skipped"] == [
        {"rule_id": 2, "reason": "no elastic deploy target is set"}
    ]
    assert session.added == []
