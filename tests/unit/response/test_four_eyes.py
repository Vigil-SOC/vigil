"""Four-eyes: nobody approves an action they proposed or one proposed for them (#2932)."""

from datetime import datetime
from typing import Optional
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api.v1.approvals_router import router as approvals_router
from core.auth.current_user import get_current_user
from core.deps import provide_approvals
from core.response import ceiling
from core.response.approval_service import (
    FOUR_EYES_REFUSAL,
    ApprovalService,
    FourEyesRefused,
)
from core.response.checkpoints import proposer_principal_id
from core.storage.models import ApprovalAction, WorkflowRun
from core.storage.unit_of_work import unit_of_work

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


@pytest.fixture
def four_eyes(monkeypatch):
    def _set(on: bool):
        monkeypatch.setattr(
            ceiling, "read_ceiling", lambda: ceiling.Ceiling(four_eyes=on)
        )

    _set(True)
    return _set


def _run(triggered_by: Optional[str], context: Optional[dict] = None) -> str:
    run_id = f"wfr-four-eyes-{uuid4().hex[:8]}"
    with unit_of_work() as session:
        session.add(
            WorkflowRun(
                run_id=run_id,
                workflow_id="hunt",
                workflow_name="hunt",
                status="completed",
                triggered_by_principal_id=triggered_by,
                trigger_context=context or {},
                started_at=datetime(2099, 1, 1),
            )
        )
    return run_id


def _seed(
    creator: Optional[str] = None,
    workflow_run_id: Optional[str] = None,
    parameters: Optional[dict] = None,
) -> str:
    action_id = f"four-eyes-{uuid4().hex[:8]}"
    with unit_of_work() as session:
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
                created_by_principal_id=creator,
                requires_approval=True,
                status="pending",
                created_at=datetime(2099, 1, 1),
                parameters=parameters or {},
                workflow_run_id=workflow_run_id,
            )
        )
    return action_id


def _approve(action_id: str, who: str):
    return ApprovalService().approve_action(
        action_id, approved_by=who, approved_by_principal_id=who
    )


def _status(action_id: str) -> str:
    return ApprovalService().get_action(action_id).status


def test_creator_is_refused_over_http_and_row_stays_pending(four_eyes, monkeypatch):
    from core.auth.auth_service import AuthService

    class _User:
        user_id = "agent:hunt"
        username = "hunt"

    monkeypatch.setattr(
        AuthService, "check_permission", staticmethod(lambda *a, **k: True)
    )
    app = FastAPI()
    app.include_router(approvals_router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: _User()
    app.dependency_overrides[provide_approvals] = ApprovalService
    action_id = _seed(creator="agent:hunt")

    response = TestClient(app).post(f"/api/approvals/{action_id}/approve", json={})

    assert response.status_code == 403
    assert response.json()["detail"] == FOUR_EYES_REFUSAL
    assert _status(action_id) == "pending"


def test_person_whose_hunt_proposed_it_is_refused(four_eyes):
    # Hunt proposals name the run in parameters, not workflow_run_id.
    run_id = _run(triggered_by="person-a")
    action_id = _seed(creator="agent:hunt", parameters={"run_id": run_id})

    with pytest.raises(FourEyesRefused):
        _approve(action_id, "person-a")
    assert _status(action_id) == "pending"


def test_person_the_linked_run_acted_for_is_refused(four_eyes):
    action_id = _seed(workflow_run_id=_run(triggered_by="person-a"))

    with pytest.raises(FourEyesRefused):
        _approve(action_id, "person-a")


def test_a_different_person_approves(four_eyes):
    run_id = _run(triggered_by="person-a")
    action_id = _seed(creator="agent:hunt", parameters={"run_id": run_id})

    assert _approve(action_id, "person-b").status == "approved"


def test_a_row_with_no_proposer_principal_approves(four_eyes):
    action_id = _seed(workflow_run_id=_run(triggered_by=None))

    assert _approve(action_id, "person-a").status == "approved"


def test_off_leaves_approval_unchanged(four_eyes):
    four_eyes(False)
    run_id = _run(triggered_by="person-a")
    action_id = _seed(creator="person-a", parameters={"run_id": run_id})

    assert _approve(action_id, "person-a").status == "approved"


def test_proposer_is_the_agent_named_on_parameters_or_the_run():
    run_id = _run(triggered_by="person-a", context={"run_kind": "hunt"})

    assert proposer_principal_id(run_id, {"agent_id": "triage"}) == "agent:triage"
    assert proposer_principal_id(run_id, {"agent_id": "agent:x"}) == "agent:x"
    assert proposer_principal_id(run_id) == "agent:hunt"
    assert proposer_principal_id(_run(triggered_by=None)) is None


# The run's directive inbox answers the same row, so it is held to the same rule.
def test_the_directive_inbox_refuses_the_person_the_run_acted_for(four_eyes):
    from core.response.approval_service import four_eyes_refuses

    run_id = _run(triggered_by="person-a")
    _seed(workflow_run_id=run_id, parameters={"checkpoint_id": "cp-1"})

    with unit_of_work() as session:
        assert four_eyes_refuses(session, run_id, "cp-1", "person-a")
        assert not four_eyes_refuses(session, run_id, "cp-1", "person-b")
    four_eyes(False)
    with unit_of_work() as session:
        assert not four_eyes_refuses(session, run_id, "cp-1", "person-a")


# A checkpoint with no pending row (not raised yet, withdrawn, or a hunt's own)
# is still the run's: its person must not approve it either.
def test_the_directive_inbox_refuses_the_run_person_with_no_pending_row(four_eyes):
    from core.response.approval_service import four_eyes_refuses

    run_id = _run(triggered_by="person-a")
    _seed(workflow_run_id=run_id, parameters={"checkpoint_id": "cp-1"})
    nobodys = _run(triggered_by=None)

    with unit_of_work() as session:
        assert four_eyes_refuses(session, run_id, "cp-other", "person-a")
        assert not four_eyes_refuses(session, run_id, "cp-other", "person-b")
        assert not four_eyes_refuses(session, nobodys, "cp-1", "person-a")
        assert not four_eyes_refuses(session, "wfr-missing", "cp-1", "person-a")
        assert not four_eyes_refuses(session, run_id, "cp-other", None)
