# Worked example of the #459 substitution pattern: a handler's service arrives
# through a Depends provider, so a test swaps in a stub via
# app.dependency_overrides and never touches a database, an LLM or an MCP process.

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DEV_MODE", "true")
os.environ.setdefault("VIGIL_CSRF_ENABLED", "false")

# Mirror backend/main.py's sys.path setup so backend/api/__init__.py resolves.
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.approvals import router as approvals_router
from backend.deps import provide_approvals, provide_workflows


def _action(action_id="ACT-1", workflow_run_id=None):
    return SimpleNamespace(
        action_id=action_id,
        action_type="isolate_host",
        title="Isolate host",
        description="stub",
        target="host-1",
        confidence=0.9,
        reason="stub",
        evidence=[],
        created_at="2026-01-01T00:00:00Z",
        created_by="pytest",
        requires_approval=True,
        status="pending",
        approved_at=None,
        approved_by=None,
        executed_at=None,
        execution_result=None,
        rejection_reason=None,
        parameters={},
        workflow_run_id=workflow_run_id,
        workflow_phase_id=None,
    )


class StubApprovals:
    def __init__(self, actions=None):
        self.actions = actions if actions is not None else [_action()]
        self.approved = []

    def list_pending_approvals(self):
        return self.actions

    def get_action(self, action_id):
        return next((a for a in self.actions if a.action_id == action_id), None)

    def approve_action(self, action_id, approved_by=None):
        self.approved.append((action_id, approved_by))
        action = self.get_action(action_id)
        action.status = "approved"
        action.approved_by = approved_by
        return action


class StubWorkflows:
    def __init__(self):
        self.resumed = []

    async def resume_workflow(self, run_id, decision, **kwargs):
        self.resumed.append((run_id, decision, kwargs))
        return {"success": True, "status": "completed", "run_id": run_id}


@pytest.fixture()
def app():
    # A bare app with just the router under test: no lifespan runs, so nothing
    # populates app.state and the overrides below are the only wiring.
    application = FastAPI()
    application.include_router(approvals_router, prefix="/api")
    return application


@pytest.mark.unit
def test_pending_approvals_reads_from_the_injected_service(app):
    stub = StubApprovals()
    app.dependency_overrides[provide_approvals] = lambda: stub

    resp = TestClient(app).get("/api/approvals/pending")

    assert resp.status_code == 200
    assert [a["action_id"] for a in resp.json()["actions"]] == ["ACT-1"]


@pytest.mark.unit
def test_missing_action_is_a_404(app):
    app.dependency_overrides[provide_approvals] = lambda: StubApprovals(actions=[])

    resp = TestClient(app).get("/api/approvals/does-not-exist")

    assert resp.status_code == 404


@pytest.mark.unit
def test_approving_a_workflow_linked_action_resumes_the_run(app):
    approvals = StubApprovals(actions=[_action(workflow_run_id="wfr-1")])
    workflows = StubWorkflows()
    app.dependency_overrides[provide_approvals] = lambda: approvals
    app.dependency_overrides[provide_workflows] = lambda: workflows

    resp = TestClient(app).post(
        "/api/approvals/ACT-1/approve", json={"approved_by": "tester"}
    )

    assert resp.status_code == 200
    assert approvals.approved == [("ACT-1", "tester")]
    # The run resumed through the injected WorkflowsService, not a real one.
    assert workflows.resumed == [("wfr-1", "approved", {"approved_by": "tester"})]
    assert resp.json()["resume_result"]["status"] == "completed"


@pytest.mark.unit
def test_unlinked_action_does_not_resume_any_workflow(app):
    approvals = StubApprovals(actions=[_action(workflow_run_id=None)])
    workflows = StubWorkflows()
    app.dependency_overrides[provide_approvals] = lambda: approvals
    app.dependency_overrides[provide_workflows] = lambda: workflows

    resp = TestClient(app).post("/api/approvals/ACT-1/approve", json={})

    assert resp.status_code == 200
    assert workflows.resumed == []
    assert resp.json()["resume_result"] is None
