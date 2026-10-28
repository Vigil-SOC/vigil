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
def client(authenticate_app):
    app = FastAPI()
    authenticate_app(app)
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
    return client.post(
        f"/api/agent-runs/{RUN}/directives",
        json=body or {"kind": "note", "text": "look at 10.0.0.5"},
    )


class TestQueueing:
    def test_accepts_a_directive_and_reports_its_id(self, client, monkeypatch):
        _queues(monkeypatch)
        response = _post(client)

        assert response.status_code == 202
        assert response.json()["directive_id"] == "dir-abc123"

    # Attribution is the point of the record: a directive nobody owns leaves the
    # ledger unable to say who steered the run.
    def test_the_actor_is_the_session_user_not_the_body(self, client, monkeypatch):
        _queues(monkeypatch)
        _post(client, {"kind": "note", "text": "x", "actor": "admin"})
        assert _queues.seen["actor"] == "test-admin"

    def test_a_directive_too_long_to_read_is_refused_unqueued(
        self, client, monkeypatch
    ):
        _queues(monkeypatch)
        _queues.seen = None
        response = _post(client, {"kind": "note", "text": "x" * 100_000})
        assert response.status_code == 422
        assert _queues.seen is None

    def test_carries_the_workflow_fields_through(self, client, monkeypatch):
        from core.response import approval_service

        monkeypatch.setattr(agent_runs_router, "deny_unless", lambda *a, **k: None)
        monkeypatch.setattr(
            approval_service, "four_eyes_refuses", lambda s, r, c, p: False
        )
        _queues(monkeypatch)
        _post(
            client,
            {"kind": "approve", "text": "go on", "fields": {"checkpoint_id": "apr-1"}},
        )
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
    def test_maps_each_refusal_to_its_own_status(
        self, client, monkeypatch, error, expected
    ):
        _raises(monkeypatch, error)
        assert _post(client).status_code == expected


class TestWhoMaySend:
    # A checkpoint answer is the Approvals screen's decision by another door, and
    # stopping someone else's run is the same call as cancelling it.
    @pytest.fixture()
    def analyst(self, monkeypatch):
        def _deny(user_id, permission, session=None, **_):
            if permission == "ai_decisions.approve":
                raise agent_runs_router.HTTPException(status_code=403)

        monkeypatch.setattr(agent_runs_router, "deny_unless", _deny)
        monkeypatch.setattr(agent_runs_router, "_started_by", lambda s, r: "someone")
        _queues(monkeypatch)
        _queues.seen = None

    @pytest.mark.parametrize("kind", ["approve", "reject", "benign"])
    def test_a_decision_needs_the_approve_permission(self, client, analyst, kind):
        body = {"kind": kind, "fields": {"checkpoint_id": "apr-1"}}
        assert _post(client, body).status_code == 403
        assert _queues.seen is None

    @pytest.mark.parametrize("kind", ["abort", "conclude"])
    def test_another_persons_run_cannot_be_stopped(self, client, analyst, kind):
        assert _post(client, {"kind": kind}).status_code == 403
        assert _queues.seen is None

    def test_the_person_who_started_the_run_may_stop_it(
        self, client, analyst, monkeypatch
    ):
        monkeypatch.setattr(agent_runs_router, "_started_by", lambda s, r: "test-admin")
        assert _post(client, {"kind": "abort"}).status_code == 202

    def test_a_note_still_needs_only_chat(self, client, analyst):
        assert _post(client).status_code == 202

    def test_four_eyes_refuses_the_person_the_run_acted_for(self, client, monkeypatch):
        from core.response import approval_service

        monkeypatch.setattr(agent_runs_router, "deny_unless", lambda *a, **k: None)
        monkeypatch.setattr(
            approval_service, "four_eyes_refuses", lambda s, r, c, p: True
        )
        _queues(monkeypatch)
        _queues.seen = None
        body = {"kind": "approve", "fields": {"checkpoint_id": "apr-1"}}
        assert _post(client, body).status_code == 403
        assert _queues.seen is None
