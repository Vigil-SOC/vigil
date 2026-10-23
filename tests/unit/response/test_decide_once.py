"""An approval is decided once; a second decision is refused, not answered 200 (#2870)."""

import logging
import threading
from contextlib import suppress
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api.v1.approvals_router import router as approvals_router
from core.auth.current_user import get_current_user
from core.deps import provide_approvals
from core.response.approval_service import AlreadyDecided, ApprovalService
from core.response.checkpoints import expire_stale, withdraw_for_run
from core.storage.models import ApprovalAction, WorkflowRun
from core.storage.unit_of_work import unit_of_work

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


class _User:
    user_id = "u-b"
    username = "analyst-b"


@pytest.fixture
def client(monkeypatch):
    from core.auth.auth_service import AuthService

    monkeypatch.setattr(AuthService, "check_permission", staticmethod(lambda *a: True))
    app = FastAPI()
    app.include_router(approvals_router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: _User()
    app.dependency_overrides[provide_approvals] = ApprovalService
    return TestClient(app)


@pytest.fixture
def resumed(monkeypatch):
    from core.workflows import run_resume

    calls = []

    async def _resume(run_id, action_id, actor):
        calls.append((run_id, action_id, actor))
        return {"success": True}

    monkeypatch.setattr(run_resume, "resume_run", _resume)
    return calls


def _seed(status: str = "pending") -> str:
    action_id = f"decide-once-{uuid4().hex[:8]}"
    run_id = f"wfr-{action_id}"
    with unit_of_work() as session:
        session.add(
            WorkflowRun(
                run_id=run_id,
                workflow_id="decide-once",
                workflow_name="decide-once",
                status="paused",
                trigger_context={},
                started_at=datetime(2099, 1, 1),
            )
        )
        session.flush()
        session.add(
            ApprovalAction(
                action_id=action_id,
                action_type="block_ip",
                title=action_id,
                description="hold",
                target="203.0.113.7",
                confidence=0.4,
                reason="because",
                evidence=[],
                created_by="pytest",
                requires_approval=True,
                status=status,
                created_at=datetime(2099, 1, 1),
                parameters={},
                workflow_run_id=run_id,
            )
        )
    return action_id


def _status(action_id: str) -> str:
    return ApprovalService().get_action(action_id).status


def test_approve_after_reject_is_409_and_does_not_resume(client, resumed, caplog):
    action_id = _seed()
    rejected = client.post(f"/api/approvals/{action_id}/reject", json={"reason": "no"})
    assert rejected.status_code == 200
    assert len(resumed) == 1

    with caplog.at_level(logging.ERROR, logger="core.storage.connection"):
        late = client.post(f"/api/approvals/{action_id}/approve", json={})

    assert late.status_code == 409
    assert late.json()["detail"] == f"Action {action_id} is already rejected"
    assert not any(
        "Database transaction failed" in rec.message for rec in caplog.records
    )
    assert len(resumed) == 1
    assert _status(action_id) == "rejected"


def test_reject_after_approve_is_409(client, resumed):
    action_id = _seed()
    assert (
        client.post(f"/api/approvals/{action_id}/approve", json={}).status_code == 200
    )

    late = client.post(f"/api/approvals/{action_id}/reject", json={"reason": "late"})

    assert late.status_code == 409
    assert len(resumed) == 1
    assert _status(action_id) == "approved"


@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_tool_path_reports_already_decided(monkeypatch, verb):
    from core.agents import tool_registry

    monkeypatch.setattr(tool_registry, "current_caller", lambda: "analyst-b")
    monkeypatch.setattr("core.cases.agent_closure.current_caller", lambda: "analyst-b")
    monkeypatch.setattr(tool_registry, "_without_approval_right", lambda: None)
    action_id = _seed(status="rejected")

    if verb == "approve":
        out = tool_registry.approve_action(action_id=action_id)
    else:
        out = tool_registry.reject_action(action_id=action_id, reason="again")

    assert out == {"error": f"Action {action_id} is already rejected"}
    assert _status(action_id) == "rejected"


class _ListedPendingThenDecided(ApprovalService):
    def __init__(self, action_id):
        super().__init__()
        self.listed = ApprovalService().get_action(action_id)

    def list_actions(self, **_kw):
        return [self.listed]

    def list_stale_pending(self, cutoff):
        return [self.listed.action_id]


def test_sweeps_skip_a_row_decided_after_it_was_listed():
    action_id = _seed(status="approved")
    service = _ListedPendingThenDecided(action_id)

    withdraw_for_run("any", "run ended", service)
    expire_stale(7, service)

    assert _status(action_id) == "approved"


def test_concurrent_approve_and_reject_decide_once():
    for _ in range(20):
        action_id = _seed()
        service = ApprovalService()
        gate = threading.Barrier(2)
        won = []

        def decide(fn):
            gate.wait()
            with suppress(AlreadyDecided):
                fn()
                won.append(fn)

        threads = [
            threading.Thread(
                target=decide, args=(lambda: service.approve_action(action_id, "a"),)
            ),
            threading.Thread(
                target=decide,
                args=(lambda: service.reject_action(action_id, "no", "b"),),
            ),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(won) == 1
