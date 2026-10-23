from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.deps import provide_approvals, provide_workflow_runs, provide_workflows
from core.workflows.custom_workflow_service import CustomWorkflowService
from core.workflows.workflows_router import router

pytestmark = pytest.mark.unit


def _app(authenticate_app):
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.state.custom_workflows = CustomWorkflowService()
    return app, authenticate_app(app)


@pytest.mark.external_service
def test_custom_workflow_author_is_the_session_user(authenticate_app):
    app, user = _app(authenticate_app)
    client = TestClient(app)
    forged = client.post(
        "/api/workflows/custom",
        json={"name": "Forged", "description": "d", "created_by": "mallory"},
    )
    omitted = client.post(
        "/api/workflows/custom", json={"name": "Omitted", "description": "d"}
    )
    assert forged.status_code == omitted.status_code == 201
    stored = CustomWorkflowService().get(forged.json()["workflow_id"])
    assert stored["created_by"] == user.username
    assert omitted.json()["created_by"] == user.username


def test_cancel_is_recorded_against_the_session_user(authenticate_app):
    app, user = _app(authenticate_app)
    runs, approvals = MagicMock(), MagicMock()
    runs.get_run.return_value = {"run_id": "r1", "status": "running"}
    approvals.list_actions.return_value = [MagicMock(action_id="a1")]
    app.dependency_overrides[provide_workflow_runs] = lambda: runs
    app.dependency_overrides[provide_approvals] = lambda: approvals
    app.dependency_overrides[provide_workflows] = lambda: MagicMock()
    with patch("core.workflows.run_cancel.stop_run", return_value={}) as stop:
        r = TestClient(app).post(
            "/api/workflows/runs/r1/cancel",
            json={"reason": "x", "rejected_by": "mallory"},
        )
    assert r.status_code == 200
    assert approvals.reject_action.call_args.kwargs["rejected_by"] == user.username
    assert stop.call_args.args[2] == user.username
