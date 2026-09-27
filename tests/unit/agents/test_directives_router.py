# The console's way into a running agent. enqueue_directive had no HTTP route at
# all, so an operator could steer a run only by writing the table by hand.

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

from core.api.v1 import agent_runs_router  # noqa: E402
from core.agents.directives import (  # noqa: E402
    InvalidDirective,
    RunAlreadyEnded,
    UnknownRun,
)
from core.routing import request_unit_of_work  # noqa: E402

pytestmark = pytest.mark.unit

RUN = "9c1c2d3e-0000-4000-8000-000000000634"


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(agent_runs_router.router, prefix="/api/agent-runs")
    app.dependency_overrides[request_unit_of_work] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


def _queues(monkeypatch, **kwargs):
    def _enqueue(session, **passed):
        _queues.seen = passed
        return {
            "directive_id": "dir-abc123",
            "kind": passed["kind"],
            "created_at": "2026-08-13T00:00:00+00:00",
        }

    monkeypatch.setattr(agent_runs_router, "enqueue_directive", _enqueue)


def _raises(monkeypatch, error):
    def _enqueue(session, **passed):
        raise error

    monkeypatch.setattr(agent_runs_router, "enqueue_directive", _enqueue)


def _post(client, body=None):
    return client.post(f"/api/agent-runs/{RUN}/directives", json=body or {"kind": "note", "text": "look at 10.0.0.5"})


class TestQueueing:
    def test_accepts_a_directive_and_reports_its_id(self, client, monkeypatch):
        _queues(monkeypatch)
        response = _post(client)

        assert response.status_code == 202
        assert response.json()["directive_id"] == "dir-abc123"

    # Attribution is the point of the record: a directive nobody owns leaves the
    # ledger unable to say who steered the run.
    def test_defaults_the_actor_rather_than_leaving_it_empty(self, client, monkeypatch):
        _queues(monkeypatch)
        _post(client)
        assert _queues.seen["actor"] == "analyst"

    def test_carries_the_workflow_fields_through(self, client, monkeypatch):
        _queues(monkeypatch)
        _post(client, {"kind": "approve", "text": "go on", "fields": {"checkpoint_id": "apr-1"}})
        assert _queues.seen["fields"] == {"checkpoint_id": "apr-1"}


class TestRefusals:
    # Told apart, because the fix for each is different: a run that never existed,
    # one that already ended, and a directive that was malformed.
    @pytest.mark.parametrize(
        "error,expected",
        [
            (UnknownRun("no such run"), 404),
            (RunAlreadyEnded("already ended"), 409),
            (InvalidDirective("unknown directive kind"), 400),
        ],
    )
    def test_maps_each_refusal_to_its_own_status(self, client, monkeypatch, error, expected):
        _raises(monkeypatch, error)
        assert _post(client).status_code == expected
