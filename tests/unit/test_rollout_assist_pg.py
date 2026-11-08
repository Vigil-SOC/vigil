"""Assist approvals on a real Postgres (#4068): a second carrier of the same
approval promotes nothing and opens no second PR, the request/approve/decline
path writes real approval and audit rows, and a second sweep asks nothing new.

Runs when VIGIL_TEST_PG_DSN names a throwaway database, e.g.
postgresql://postgres:pg@127.0.0.1:55468/postgres."""

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest
from sqlalchemy import func, select, text

from core.detections.inventory import readers, rollout, rollout_approval
from core.response.approval_service import ActionStatus, ApprovalService
from core.storage.models import (
    ApprovalAction,
    ConfigAuditLog,
    DetectionRule,
    DetectionRuleStage,
    DetectionRuleVersion,
)

DSN = os.environ.get("VIGIL_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="VIGIL_TEST_PG_DSN not set")
TARGET = {"git_url": "https://github.com/acme/detections.git", "subdirectory": "rules"}


@pytest.fixture
def db(monkeypatch):
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
        for table in (
            "approval_actions",
            "detection_rule_stages",
            "detection_rule_versions",
            "detection_rules",
        ):
            session.execute(text(f"DELETE FROM {table}"))
        session.execute(
            text(
                "DELETE FROM config_audit_log "
                "WHERE config_key IN ('rule_approval', 'overlay_deploy')"
            )
        )
        session.execute(text("DELETE FROM system_config WHERE key='autonomy.ceiling'"))
    yield manager
    manager._engine.dispose()
    connection.DatabaseManager._instance = None


@pytest.fixture
def prs(monkeypatch, db):
    """GitHub is faked: each PR a carrier opens is recorded, slowly enough that
    a second carrier would overlap the first."""
    opened = []

    def open_pr(copy, target, body, token):
        time.sleep(0.02)
        opened.append(copy.rule_id)
        return {"pr_url": f"https://github.com/acme/detections/pull/{copy.rule_id}"}

    monkeypatch.setattr(rollout, "_open_pr", open_pr)
    monkeypatch.setattr(rollout, "_deploy_targets", lambda: {"elastic": TARGET})
    import core.secrets

    monkeypatch.setattr(core.secrets, "get_secret", lambda name: "token")
    return opened


def _seed(db, native_key="r1"):
    now = datetime.now(timezone.utc)
    week = [(now - timedelta(days=i)).date().isoformat() for i in range(30)]
    with db.session_scope() as session:
        rule = DetectionRule(
            source=readers.OVERLAY_SOURCE,
            native_key=native_key,
            name=f"rule {native_key}",
            language="elastic",
            latest_version=2,
            path=f"{native_key}.toml",
        )
        session.add(rule)
        session.flush()
        session.add(
            DetectionRuleVersion(
                rule_id=rule.id,
                version=2,
                fingerprint="f",
                content={"_content": "[rule]\nname = 'x'\n"},
                checks={
                    "runs": {"days": {d: {"pass": 4, "fail": 0} for d in week}},
                    "shadow": {"days": {d: 3 for d in week}},
                    "sees": {"result": "pass"},
                    "lint": {"result": "pass", "findings": []},
                },
            )
        )
        session.add(
            DetectionRuleStage(
                rule_id=rule.id,
                version=2,
                stage="shadow",
                actor="system",
                reason="new",
                at=now - timedelta(days=8),
            )
        )
        return rule.id


def _sweep():
    report = rollout.RolloutReport()
    rollout.sweep(datetime.now(timezone.utc), report)
    return report


def _approved(db, rule_id, who="dana"):
    service = ApprovalService()
    (row,) = [
        a
        for a in service.list_actions(status=ActionStatus.PENDING)
        if a.parameters.get("rule_id") == rule_id
    ]
    return service.approve_action(row.action_id, approved_by=who)


def _live_rows(db, rule_id):
    with db.session_scope() as session:
        return session.scalar(
            select(func.count())
            .select_from(DetectionRuleStage)
            .where(
                DetectionRuleStage.rule_id == rule_id,
                DetectionRuleStage.stage == "live",
            )
        )


def _audit(db, rule_id, event):
    with db.session_scope() as session:
        rows = session.scalars(
            select(ConfigAuditLog)
            .where(
                ConfigAuditLog.config_key == "rule_approval",
                ConfigAuditLog.action == event,
            )
            .order_by(ConfigAuditLog.id)
        ).all()
        return [
            (r.changed_by, r.change_reason)
            for r in rows
            if r.new_value["rule_id"] == rule_id
        ]


def test_a_second_sweep_asks_nothing_new(db, prs):
    rid = _seed(db)
    assert _sweep().asked == [f"{rid} v2"]
    assert _sweep().asked == []
    with db.session_scope() as session:
        rows = session.scalars(select(ApprovalAction)).all()
        assert len(rows) == 1 and rows[0].status == "pending"
        assert rows[0].action_type == "promote_rule"
        assert "days in shadow" in rows[0].reason
    assert _live_rows(db, rid) == 0 and prs == []
    assert [who for who, _ in _audit(db, rid, "request")] == ["system"]


def test_approve_writes_the_stage_pr_and_audit_rows_with_who(db, prs):
    rid = _seed(db)
    _sweep()
    approved = _approved(db, rid, "dana")
    rollout_approval.on_decided(approved, "dana")
    with db.session_scope() as session:
        (stage,) = session.scalars(
            select(DetectionRuleStage).where(DetectionRuleStage.stage == "live")
        ).all()
        assert (stage.actor, stage.rule_id) == ("dana", rid)
        assert stage.reason.startswith("approved by dana")
        row = session.scalars(select(ApprovalAction)).one()
        assert (row.status, row.approved_by) == ("executed", "dana")
    assert prs == [rid]
    assert [who for who, _ in _audit(db, rid, "approve")] == ["dana"]
    _sweep()  # nothing is repeated
    assert prs == [rid] and _live_rows(db, rid) == 1


def test_decline_keeps_shadow_and_records_who_and_why(db, prs):
    rid = _seed(db)
    _sweep()
    service = ApprovalService()
    (pending,) = service.list_actions(status=ActionStatus.PENDING)
    declined = service.reject_action(pending.action_id, "too noisy", "dana")
    rollout_approval.on_decided(declined, "dana", "too noisy")
    with db.session_scope() as session:
        row = session.scalars(select(ApprovalAction)).one()
        assert (row.status, row.rejection_reason) == ("rejected", "too noisy")
    assert _live_rows(db, rid) == 0 and prs == []
    assert _audit(db, rid, "decline") == [("dana", "too noisy")]
    assert _sweep().asked == [] and _live_rows(db, rid) == 0


@pytest.mark.parametrize("round_", range(25))
def test_approve_racing_the_sweep_promotes_and_opens_one_pr(
    db, prs, monkeypatch, round_
):
    rid = _seed(db, f"race-{round_}")
    _sweep()
    approved = _approved(db, rid, "dana")
    real_copies = rollout._copies

    def slow_copies():  # widens the window between reading a copy and acting on it
        copies = real_copies()
        time.sleep(0.01)
        return copies

    monkeypatch.setattr(rollout, "_copies", slow_copies)
    gate = threading.Barrier(2)
    errors = []

    def go(work):
        try:
            gate.wait()
            work()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(
            target=go, args=(lambda: rollout_approval.on_decided(approved, "dana"),)
        ),
        threading.Thread(
            target=go,
            args=(
                lambda: rollout_approval.carry_out_approved(rollout.RolloutReport()),
            ),
        ),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert _live_rows(db, rid) == 1
    assert prs == [rid]
    with db.session_scope() as session:
        assert session.scalars(select(ApprovalAction)).one().status == "executed"
    assert len(_audit(db, rid, "approve")) == 1
