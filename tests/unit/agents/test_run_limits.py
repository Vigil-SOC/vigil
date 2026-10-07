# What a caller may ask of a run's budget. withOverrides only checks that a value
# is positive, so the ceiling on a caller's ask is enforced before the job is queued.

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.agents.run_limits import MAX_COST_USD, MAX_ITERATIONS
from core.api.v1 import agent_runs_router
from core.routing import request_unit_of_work
from core.workflows.workflows_service import _asked_iterations, _asked_overrides

pytestmark = pytest.mark.unit

BODY = {"run_kind": "hunt", "playbook": "p.md", "config": "c.yaml"}


@pytest.fixture()
def client(monkeypatch, authenticate_app):
    queued = []

    async def _enqueue(job):
        queued.append(job)
        return "job-1"

    monkeypatch.setattr(agent_runs_router, "enqueue_run", _enqueue)
    monkeypatch.setattr(agent_runs_router, "_begin_run_row", lambda *a, **k: None)
    app = FastAPI()
    authenticate_app(app)
    app.include_router(agent_runs_router.router, prefix="/api/agent-runs")
    app.dependency_overrides[request_unit_of_work] = lambda: None
    http = TestClient(app, raise_server_exceptions=False)
    http.queued = queued
    return http


def _start(client, overrides):
    return client.post("/api/agent-runs", json={**BODY, "overrides": overrides})


class TestStartRun:
    def test_a_raise_within_the_ceiling_is_queued(self, client):
        overrides = {"budgets": {"max_cost_usd": 25, "max_calls": 40}}
        assert _start(client, overrides).status_code == 202
        assert client.queued[0]["request"]["overrides"] == overrides

    @pytest.mark.parametrize(
        "overrides",
        [
            {"budgets": {"max_cost_usd": 100_000}},
            {"budgets": {"max_calls": 100_000}},
            {"budgets": {"max_wall_ms": 864_000_000}},
            {"runtime": {"max_turns": 100_000}},
            {"budgets": {"max_cost_usd": 1e300}},
            {"budgets": {"max_cost_usd": -1}},
            {"budgets": {"max_cost_usd": True}},
            {"budgets": {"max_cost_usd": "5"}},
            {"budgets": {"max_iterations": 5}},
            {"thresholds": {"hard_max_cost_usd": 5}},
            {"budgets": [1]},
        ],
    )
    def test_an_ask_past_the_ceiling_is_refused_before_queueing(
        self, client, overrides
    ):
        assert _start(client, overrides).status_code == 422
        assert client.queued == []


class TestConsoleAsks:
    def test_cost_is_clamped_to_the_ceiling(self):
        assert _asked_overrides({"max_cost_usd": 1e9}) == {
            "budgets": {"max_cost_usd": MAX_COST_USD}
        }
        assert _asked_overrides({"max_cost_usd": 12}) == {
            "budgets": {"max_cost_usd": 12.0}
        }
        assert _asked_overrides({"max_cost_usd": float("inf")}) is None

    def test_turns_are_clamped_to_the_ceiling(self):
        assert _asked_iterations({"iterations": 10**6}) == MAX_ITERATIONS
        assert _asked_iterations({"iterations": 8}) == 8
        assert _asked_iterations({"iterations": float("inf")}) is None
