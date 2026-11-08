"""Under Assist nothing goes live and no deploy PR opens without a person (#4068).

The sweep puts a due promotion to a person as one approval request; approving
promotes the rule and opens its PR, declining leaves it in shadow. A manual
deploy asks first unless the person deploying may approve. Act is unchanged,
and an unreadable ceiling moves nothing."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.detections.inventory import rollout, rollout_approval, sync
from core.response import ceiling
from core.response.approval_service import ActionStatus, PendingAction

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
TARGET = {"git_url": "https://github.com/acme/detections.git", "subdirectory": "rules"}


def _week(days):
    return [(NOW - timedelta(days=i)).date().isoformat() for i in range(days)]


def _copy(rule_id=1, stage="shadow", days_in=8):
    return rollout.Copy(
        rule_id=rule_id,
        version=2,
        name=f"rule {rule_id}",
        language="elastic",
        stage=stage,
        stage_at=NOW - timedelta(days=days_in),
        checks={
            "runs": {"days": {d: {"pass": 4, "fail": 0} for d in _week(30)}},
            "shadow": {"days": {d: 3 for d in _week(30)}},
            "sees": {"result": "pass"},
            "lint": {"result": "pass", "findings": []},
        },
        rule={},
        key=f"key-{rule_id}",
        toml="[rule]\n",
        path=f"{rule_id}.toml",
    )


class FakeApprovals:
    """The approval queue, in memory and shared across instances."""

    rows = {}

    def _put_action(self, **kw):
        key = kw["idempotency_key"]
        for row in self.rows.values():
            if row.idempotency_key == key:
                return row, False
        row = self._row(**kw)
        return row, True

    def create_action(self, **kw):
        kw.setdefault("idempotency_key", None)
        return self._row(**kw)

    def _row(self, **kw):
        row = PendingAction(
            action_id=f"action-{len(self.rows) + 1}",
            action_type=kw["action_type"].value,
            title=kw["title"],
            description=kw["description"],
            target=kw["target"],
            confidence=0.0,
            reason=kw["reason"],
            evidence=[],
            created_at=NOW,
            created_by=kw.get("created_by", "system"),
            requires_approval=True,
            status="pending",
            parameters=kw["parameters"],
        )
        row.idempotency_key = kw["idempotency_key"]
        self.rows[row.action_id] = row
        return row

    def list_actions(self, status=None, action_type=None, **_):
        return [
            r
            for r in self.rows.values()
            if r.status == status.value and r.action_type == action_type.value
        ]

    def mark_executed(self, action_id, result):
        self.rows[action_id].status = "executed"

    def mark_failed(self, action_id, error):
        self.rows[action_id].status = "failed"


class World:
    def __init__(self, monkeypatch, copies, act=False):
        self.copies = copies
        self.stage_rows = []
        self.deploys = []
        self.audit = []
        self.asks = not act
        FakeApprovals.rows = {}

        class _Db:
            @contextmanager
            def session_scope(self):
                yield SimpleNamespace(
                    scalar=lambda q: 0,
                    execute=lambda q, rows=None: None,
                )

        monkeypatch.setattr(rollout, "get_db_manager", lambda: _Db())
        monkeypatch.setattr(rollout_approval, "get_db_manager", lambda: _Db())
        monkeypatch.setattr(
            sync, "_insert_stages", lambda s, rows, before: self.stage_rows.extend(rows)
        )
        monkeypatch.setattr(rollout, "read_limits", lambda: rollout.DEFAULT_LIMITS)
        monkeypatch.setattr(rollout, "_stopped", lambda: False)
        monkeypatch.setattr(rollout, "_asks_first", lambda: self.asks)
        monkeypatch.setattr(rollout, "handed_off", lambda ids: set())
        monkeypatch.setattr(rollout, "_deploy_targets", lambda: {"elastic": TARGET})
        monkeypatch.setattr(rollout, "_noise", lambda s, c: (0, 0))
        monkeypatch.setattr(rollout, "_copies", lambda: self.copies)
        monkeypatch.setattr(rollout, "_deploy", self.deploy)
        monkeypatch.setattr(rollout_approval, "ApprovalService", FakeApprovals)

        @contextmanager
        def claimed(action_id):
            row = FakeApprovals.rows[action_id]
            ok = row.status == "approved"
            yield (object(), row) if ok else (None, None)

        def finish(action_id, status, result):
            FakeApprovals.rows[action_id].status = status.value

        monkeypatch.setattr(rollout_approval, "_claimed", claimed)
        monkeypatch.setattr(rollout_approval, "_finish", finish)
        monkeypatch.setattr(rollout_approval, "_lease", lambda row: "holder")
        monkeypatch.setattr(rollout_approval, "_release", lambda aid, holder: None)
        monkeypatch.setattr(
            rollout_approval, "_latest_stage", lambda s, rid: self.copies[0].stage
        )
        monkeypatch.setattr(
            rollout_approval,
            "_audit",
            lambda event, rule_id, who, reason, **more: self.audit.append(
                (event, rule_id, who, reason)
            ),
        )

    def deploy(self, copies, targets, now, report, actor="system"):
        self.deploys.extend((c.rule_id, actor) for c in copies)
        return {c.rule_id: {"version": c.version, "pr_url": "pr"} for c in copies}

    def sweep(self):
        report = rollout.RolloutReport()
        rollout.sweep(NOW, report)
        return report


@pytest.fixture
def assist(monkeypatch):
    return World(monkeypatch, [_copy()])


def _only_request():
    (row,) = FakeApprovals.rows.values()
    return row


def test_assist_asks_instead_of_promoting_and_opens_no_pr(assist):
    report = assist.sweep()
    assert assist.stage_rows == [] and assist.deploys == []
    assert report.moves == [] and report.asked == ["1 v2"]
    row = _only_request()
    assert row.action_type == "promote_rule" and row.target == "1"
    assert "days in shadow" in row.reason  # why the checks passed
    assert row.parameters["proof"]["sees"]["result"] == "pass"
    assert assist.audit[0][:3] == ("request", 1, "system")


def test_later_sweeps_do_not_ask_again(assist):
    for _ in range(3):
        assist.sweep()
    assert len(FakeApprovals.rows) == 1 and len(assist.audit) == 1


def test_approving_promotes_and_opens_the_pr(assist):
    assist.sweep()
    row = _only_request()
    row.status, row.approved_by = ActionStatus.APPROVED.value, "dana"
    rollout_approval.on_decided(row, "dana")
    (stage,) = assist.stage_rows
    assert (stage["stage"], stage["actor"], stage["rule_id"]) == ("live", "dana", 1)
    assert stage["reason"].startswith("approved by dana")
    assert assist.deploys == [(1, "dana")]
    assert row.status == "executed"
    assert ("approve", 1, "dana") == assist.audit[-1][:3]


def test_declining_keeps_it_in_shadow_and_records_who_and_why(assist):
    assist.sweep()
    row = _only_request()
    row.status = ActionStatus.REJECTED.value
    rollout_approval.on_decided(row, "dana", "noisy on Fridays")
    assert assist.stage_rows == [] and assist.deploys == []
    assert assist.audit[-1] == ("decline", 1, "dana", "noisy on Fridays")
    assist.sweep()  # the declined stay is not asked about again
    assert len(FakeApprovals.rows) == 1 and assist.stage_rows == []


def test_an_approval_lands_on_a_sweep_after_stop_lifts(assist, monkeypatch):
    assist.sweep()
    row = _only_request()
    row.status, row.approved_by = ActionStatus.APPROVED.value, "dana"
    monkeypatch.setattr(rollout, "_stopped", lambda: True)
    rollout_approval.on_decided(row, "dana")
    assert assist.stage_rows == [] and row.status == "approved"
    monkeypatch.setattr(rollout, "_stopped", lambda: False)
    assist.sweep()
    assert len(assist.stage_rows) == 1 and assist.deploys == [(1, "dana")]
    assist.sweep()
    assert len(assist.stage_rows) == 1  # done once


def test_a_rule_reverted_meanwhile_is_not_promoted_by_a_late_yes(assist):
    assist.sweep()
    row = _only_request()
    row.status, row.approved_by = ActionStatus.APPROVED.value, "dana"
    assist.copies[0].held = True
    rollout_approval.on_decided(row, "dana")
    assert assist.stage_rows == [] and row.status == "failed"


def test_assist_opens_no_pr_for_a_rule_already_live(monkeypatch):
    world = World(monkeypatch, [_copy(stage="live", days_in=1)])
    world.sweep()
    assert world.deploys == [] and FakeApprovals.rows == {}


def test_act_promotes_and_opens_the_pr_as_before(monkeypatch):
    world = World(monkeypatch, [_copy()], act=True)
    report = world.sweep()
    (stage,) = world.stage_rows
    assert stage["stage"] == "live" and stage["actor"] == "system"
    assert report.moves == ["1 v2: shadow -> live"] and report.asked == []
    assert FakeApprovals.rows == {} and world.deploys == [(1, "system")]


# --- the manual deploy -------------------------------------------------------


def _manual(monkeypatch, approver):
    live = _copy(stage="live", days_in=1)
    world = World(monkeypatch, [live])
    monkeypatch.setattr(rollout, "_read_splunk_files", lambda c: None)
    result = rollout.deploy_overlay_only([1], actor="sam", approver=approver)
    return world, result


def test_a_manual_deploy_under_assist_asks_first(monkeypatch):
    world, result = _manual(monkeypatch, approver=False)
    assert world.deploys == [] and result["opened"] == []
    (asked,) = result["asked"]
    assert asked["rule_id"] == 1
    row = _only_request()
    assert row.action_type == "deploy_rule" and row.created_by == "sam"
    again = rollout.deploy_overlay_only([1], actor="sam")
    assert again["asked"][0]["action_id"] == row.action_id
    assert len(FakeApprovals.rows) == 1
    assert world.audit[0][:3] == ("request", 1, "sam")


def test_approving_a_deploy_request_opens_the_pr(monkeypatch):
    world, _ = _manual(monkeypatch, approver=False)
    row = _only_request()
    row.status, row.approved_by = ActionStatus.APPROVED.value, "dana"
    rollout_approval.on_decided(row, "dana")
    assert world.deploys == [(1, "dana")] and row.status == "executed"


def test_a_manual_deploy_by_an_approver_just_opens_the_pr(monkeypatch):
    world, result = _manual(monkeypatch, approver=True)
    assert world.deploys == [(1, "sam")] and result["asked"] == []
    assert FakeApprovals.rows == {}


def test_a_manual_deploy_under_act_opens_the_pr(monkeypatch):
    live = _copy(stage="live", days_in=1)
    world = World(monkeypatch, [live], act=True)
    result = rollout.deploy_overlay_only([1], actor="sam")
    assert world.deploys == [(1, "sam")] and result["asked"] == []


# --- reading the autonomy setting -------------------------------------------


def test_an_unreadable_ceiling_moves_nothing(monkeypatch):
    real_stopped, real_asks = rollout._stopped, rollout._asks_first
    world = World(monkeypatch, [_copy(), _copy(2, stage="live", days_in=1)])
    monkeypatch.setattr(rollout, "_stopped", real_stopped)
    monkeypatch.setattr(rollout, "_asks_first", real_asks)
    monkeypatch.setattr(ceiling, "try_read_stored_ceiling", lambda: None)
    report = world.sweep()
    assert world.stage_rows == [] and world.deploys == []
    assert FakeApprovals.rows == {} and report.held == 1


@pytest.mark.parametrize(
    "stored, asks",
    [
        (ceiling.Ceiling(act_reversible=True), False),
        (ceiling.Ceiling(act_reversible=False), True),
        (ceiling.Ceiling(act_reversible=True, stopped=True), True),
    ],
)
def test_asks_first_follows_the_stored_ceiling(monkeypatch, stored, asks):
    monkeypatch.setattr(ceiling, "try_read_stored_ceiling", lambda: (stored, True))
    monkeypatch.setattr(ceiling, "environment_locks_act", lambda s=None: False)
    assert rollout._asks_first() is asks


def test_the_pr_lease_outlasts_the_slowest_deploy():
    slowest = rollout.DEPLOY_TIMEOUT * 10 + sum(rollout.RACE_WAITS)
    assert rollout_approval.PR_LEASE.total_seconds() > slowest
