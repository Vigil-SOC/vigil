# GET /api/agent-runs/{id} used to answer 404 for any run without ledger events,
# so a run the worker had not picked up yet looked identical to one that never
# existed -- accepted by POST, then "no such run" (#970).

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

from core.agents import agent_runs_router  # noqa: E402
from core.routing import request_unit_of_work  # noqa: E402

pytestmark = pytest.mark.unit

RUN = "9c1c2d3e-0000-4000-8000-000000000970"


class FakeSession:
    """Answers the three reads get_run makes from the state it is given."""

    def __init__(self, events=0, terminal=None, run_row=False):
        self.events, self.terminal, self.run_row = events, terminal, run_row

    def execute(self, statement, params):
        sql = str(statement)
        if "workflow_runs" in sql:
            return _one(1 if self.run_row else None)
        if "kind = 'terminal'" in sql:
            return _one(SimpleNamespace(payload=self.terminal) if self.terminal else None)
        return _one(SimpleNamespace(events=self.events))


def _one(value):
    return SimpleNamespace(one_or_none=lambda: value)


def _client(session):
    app = FastAPI()
    app.include_router(agent_runs_router.router, prefix="/api/agent-runs")
    app.dependency_overrides[request_unit_of_work] = lambda: session
    return TestClient(app, raise_server_exceptions=False)


class TestStatus:
    def test_an_accepted_run_with_no_events_is_queued_not_missing(self):
        response = _client(FakeSession(run_row=True)).get(f"/api/agent-runs/{RUN}")

        assert response.status_code == 200
        assert response.json() == {
            "run_id": RUN,
            "status": "queued",
            "events": 0,
            "outcome": None,
            "reason": None,
        }

    def test_a_run_nobody_accepted_is_still_not_found(self):
        response = _client(FakeSession()).get(f"/api/agent-runs/{RUN}")
        assert response.status_code == 404

    # Once the worker has written to the ledger, workflow_runs no longer decides.
    def test_events_on_the_ledger_mean_running(self):
        response = _client(FakeSession(events=3, run_row=True)).get(f"/api/agent-runs/{RUN}")
        assert response.json()["status"] == "running"
        assert response.json()["events"] == 3

    def test_a_terminal_event_carries_its_outcome(self):
        session = FakeSession(events=5, terminal={"outcome": "completed", "reason": "done"})
        body = _client(session).get(f"/api/agent-runs/{RUN}").json()
        assert body["status"] == "terminal"
        assert body["outcome"] == "completed"
        assert body["reason"] == "done"
