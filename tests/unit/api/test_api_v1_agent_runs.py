"""GET /api/v1/agent-runs (list_runs) — the new endpoint and its source filter.

New endpoint + new WorkflowRunService.list_runs(workflow_source=...) filter, so
it gets direct coverage: the handler asks only for agent-sourced runs, and maps
each row to RunListItem (run_kind lifted out of trigger_context).
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-for-prod")
os.environ.setdefault("DEV_MODE", "true")

pytestmark = pytest.mark.unit


def test_list_runs_filters_to_agent_source_and_maps_rows(monkeypatch):
    from services.api.main import app
    from services.api.middleware.auth import get_current_active_user
    import core.workflows.workflow_run_service as wrs

    class _User:
        is_active = True
        username = "t"
        user_id = "t"

    captured = {}

    def fake_list_runs(self, **kwargs):
        captured.update(kwargs)
        return [
            {
                "run_id": "r1",
                "status": "completed",
                "triggered_by": "api",
                "started_at": "2026-01-01T00:00:00",
                "finished_at": "2026-01-01T00:05:00",
                "trigger_context": {"run_kind": "hunt", "prompt": "x"},
            },
            {
                "run_id": "r2",
                "status": "running",
                "triggered_by": "api",
                "started_at": "2026-01-02T00:00:00",
                "finished_at": None,
                "trigger_context": {},
            },
        ]

    monkeypatch.setattr(wrs.WorkflowRunService, "list_runs", fake_list_runs)
    app.dependency_overrides[get_current_active_user] = lambda: _User()
    try:
        resp = TestClient(app).get("/api/v1/agent-runs")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # asked for agent-sourced runs only
        assert captured.get("workflow_source") == "agent"
        # mapped shape
        assert body["count"] == 2
        assert body["runs"][0]["run_id"] == "r1"
        assert body["runs"][0]["run_kind"] == "hunt"  # lifted from trigger_context
        assert body["runs"][1]["run_kind"] is None     # missing → None, not a crash
    finally:
        app.dependency_overrides.pop(get_current_active_user, None)


def test_list_runs_also_served_at_legacy_prefix(monkeypatch):
    from services.api.main import app
    from services.api.middleware.auth import get_current_active_user
    import core.workflows.workflow_run_service as wrs

    class _User:
        is_active = True
        username = "t"
        user_id = "t"

    monkeypatch.setattr(wrs.WorkflowRunService, "list_runs", lambda self, **k: [])
    app.dependency_overrides[get_current_active_user] = lambda: _User()
    try:
        client = TestClient(app)
        assert client.get("/api/v1/agent-runs").status_code == 200
        assert client.get("/api/agent-runs").status_code == 200
    finally:
        app.dependency_overrides.pop(get_current_active_user, None)
