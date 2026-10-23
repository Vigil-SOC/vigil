from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth.current_user import get_current_user
from core.workflows import workflows_router
from core.workflows.workflow_run_service import WorkflowRunService

pytestmark = pytest.mark.external_service


def _cancel(runs: WorkflowRunService, run_id: str):
    app = FastAPI()
    app.include_router(workflows_router.router, prefix="/api")
    app.state.workflow_runs = runs
    app.state.approvals = MagicMock()
    app.state.approvals.list_actions.return_value = []
    app.state.workflows = MagicMock()
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        user_id="u1", username="analyst"
    )
    counter = MagicMock()
    with patch(
        "core.auth.permissions.AuthService.check_permission", return_value=True
    ), patch(
        "core.workflows.workflow_run_service._runs_finished_counter",
        return_value=counter,
    ):
        resp = TestClient(app).post(
            f"/api/workflows/runs/{run_id}/cancel", json={"reason": "double click"}
        )
    return resp, counter


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
def test_cancel_refuses_a_finished_run(terminal):
    runs = WorkflowRunService()
    run_id = runs.begin_run(workflow_id="wf-cancel", workflow_name="cancel")
    assert runs.finalize_run(run_id, status=terminal, error=None)
    before = runs.get_run(run_id)

    resp, counter = _cancel(runs, run_id)

    assert resp.status_code == 409
    after = runs.get_run(run_id)
    assert after["status"] == terminal
    assert after["finished_at"] == before["finished_at"]
    assert after["duration_ms"] == before["duration_ms"]
    assert after.get("error") == before.get("error")
    counter.add.assert_not_called()


def test_cancel_still_cancels_a_running_run():
    runs = WorkflowRunService()
    run_id = runs.begin_run(workflow_id="wf-cancel", workflow_name="cancel")

    resp, counter = _cancel(runs, run_id)

    assert resp.status_code == 200, resp.text
    assert runs.get_run(run_id)["status"] == "cancelled"
    counter.add.assert_called_once()
