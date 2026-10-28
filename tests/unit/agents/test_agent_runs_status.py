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

from core.api.v1 import agent_runs_router  # noqa: E402
from core.routing import request_unit_of_work  # noqa: E402

pytestmark = pytest.mark.unit

RUN = "9c1c2d3e-0000-4000-8000-000000000970"


class FakeSession:
    """Answers the three reads get_run makes from the state it is given."""

    def __init__(self, events=0, terminal=None, run_row=False):
        self.events, self.terminal = events, terminal
        # run_row: False for none, True for a queued row, or a dict of its columns.
        self.run_row = {"status": "queued"} if run_row is True else run_row

    def execute(self, statement, params):
        sql = str(statement)
        if "workflow_runs" in sql:
            if not self.run_row:
                return _one(None)
            columns = {"outcome": None, "error": None, "reason": None}
            return _one(SimpleNamespace(**{**columns, **self.run_row}))
        if "kind = 'terminal'" in sql:
            return _one(
                SimpleNamespace(payload=self.terminal) if self.terminal else None
            )
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
        response = _client(FakeSession(events=3, run_row=False)).get(
            f"/api/agent-runs/{RUN}"
        )
        assert response.json()["status"] == "running"
        assert response.json()["events"] == 3

    # workflow_runs.run_id is text, so a run must be found under any UUID spelling.
    def test_a_queued_run_is_found_under_an_uppercase_id(self):
        response = _client(FakeSession(run_row=True)).get(
            f"/api/agent-runs/{RUN.upper()}"
        )
        assert response.status_code == 200
        assert response.json()["run_id"] == RUN

    def test_a_terminal_event_carries_its_outcome(self):
        session = FakeSession(
            events=5, terminal={"outcome": "completed", "reason": "done"}
        )
        body = _client(session).get(f"/api/agent-runs/{RUN}").json()
        assert body["status"] == "terminal"
        assert body["outcome"] == "completed"
        assert body["reason"] == "done"

    # A run that fails before its ledger opens is finalised on workflow_runs by the
    # mirror; reading only the ledger reported it queued forever (#3259).
    def test_a_run_that_failed_before_its_ledger_is_terminal_not_queued(self):
        row = {
            "status": "failed",
            "outcome": "failed",
            "error": "its spec cannot be built: bad",
            "reason": "its spec cannot be built: bad",
        }
        body = _client(FakeSession(run_row=row)).get(f"/api/agent-runs/{RUN}").json()
        assert body == {
            "run_id": RUN,
            "status": "terminal",
            "events": 0,
            "outcome": "failed",
            "reason": "its spec cannot be built: bad",
        }

    # A row the API finalised itself (queue down) has an error but no outcome.
    def test_a_run_finalised_without_an_outcome_reports_its_status(self):
        row = {"status": "failed", "error": "run queue unavailable"}
        body = _client(FakeSession(run_row=row)).get(f"/api/agent-runs/{RUN}").json()
        assert (body["status"], body["outcome"]) == ("terminal", "failed")
        assert body["reason"] == "run queue unavailable"

    def test_a_running_row_with_no_events_is_still_queued(self):
        session = FakeSession(run_row={"status": "running"})
        body = _client(session).get(f"/api/agent-runs/{RUN}").json()
        assert body["status"] == "queued"
