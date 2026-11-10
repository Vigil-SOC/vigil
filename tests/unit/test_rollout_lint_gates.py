"""A rule copy goes live, stays live and gets its deploy PR only on a lint
pass: no lint result yet holds it in shadow, a lint fail turns it off from
shadow or live, and the sweep opens no PR for a live copy without one."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from core.detections.inventory import rollout

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
FAIL = {"result": "fail", "findings": [{"kind": "host", "value": "dc01"}]}


def _copy(rule_id=1, stage="shadow", lint=None, days_in=8):
    week = rollout._week(NOW)
    checks = {
        "runs": {"days": {d: {"pass": 4, "fail": 0} for d in week}},
        "shadow": {"days": {d: 3 for d in week}},
        "sees": {"result": "pass"},
        "replay": {"result": "pass", "source": "siem"},
    }
    if lint is not None:
        checks["lint"] = lint
    return rollout.Copy(
        rule_id=rule_id,
        version=2,
        name=f"rule {rule_id}",
        language="elastic",
        stage=stage,
        stage_at=NOW - timedelta(days=days_in),
        checks=checks,
        rule={},
        key=f"key-{rule_id}",
        toml="[rule]\n",
        path=f"{rule_id}.toml",
    )


def test_a_quiet_passing_week_goes_live_with_a_lint_pass():
    stage, reason, proof = rollout._due(None, _copy(lint={"result": "pass"}), NOW, 0)
    assert stage == "live"
    assert "lint passed" in reason
    assert proof["lint"] == {"result": "pass"}


def test_no_lint_result_yet_holds_it_in_shadow():
    assert rollout._due(None, _copy(lint=None), NOW, 0) is None


@pytest.mark.parametrize("stage", ["shadow", "live"])
def test_a_lint_fail_turns_it_off(stage):
    stage_to, reason, proof = rollout._due(None, _copy(stage=stage, lint=FAIL), NOW, 0)
    assert stage_to == "off"
    assert reason == "fails lint: keyed to host dc01"
    assert proof == {"lint": FAIL}


def test_a_lint_fail_turns_it_off_before_its_week_is_up():
    assert rollout._due(None, _copy(lint=FAIL, days_in=1), NOW, 0)[0] == "off"


def test_the_sweep_opens_no_pr_for_a_live_copy_without_a_lint_pass(monkeypatch):
    copies = [
        _copy(1, stage="live", lint={"result": "pass"}),
        _copy(2, stage="live", lint=None),
        _copy(3, stage="live", lint={"result": "pending"}),
    ]
    deployed = []

    class _Session:
        def scalar(self, _stmt):
            return 0

        def execute(self, *_a, **_k):
            raise AssertionError("no stage rows are due")

    class _Db:
        @contextmanager
        def session_scope(self):
            yield _Session()

    monkeypatch.setattr(rollout, "_copies", lambda: copies)
    monkeypatch.setattr(rollout, "handed_off", lambda ids: set())
    monkeypatch.setattr(rollout, "_stopped", lambda: False)
    monkeypatch.setattr(rollout, "_asks_first", lambda: False)  # Act
    monkeypatch.setattr(rollout, "_deploy_targets", lambda: {"elastic": {}})
    monkeypatch.setattr(rollout, "get_db_manager", lambda: _Db())
    monkeypatch.setattr(rollout, "_due", lambda *_a: None)
    monkeypatch.setattr(
        rollout,
        "_deploy",
        lambda live, targets, now, report: deployed.extend(c.rule_id for c in live),
    )
    rollout.sweep(NOW, rollout.RolloutReport())
    assert deployed == [1]
