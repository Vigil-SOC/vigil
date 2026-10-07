# POST /workflows/threat-hunt/coverage is the same function as the agent tool,
# and it never reaches execute_workflow (#903).

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.memory import hunt_coverage
from core.workflows import workflows_router as router
from core.workflows.workflows_service import WorkflowsService

pytestmark = pytest.mark.unit

IP = "ip:203.0.113.7"
PATH = "/api/workflows/threat-hunt/coverage"


@pytest.fixture
def client(monkeypatch):
    """The workflows router alone, mounted as the app mounts it, over a patched
    prior-hunts read. Any reach for execute_workflow fails the test."""
    monkeypatch.setattr(
        hunt_coverage,
        "list_prior_hunts",
        lambda keys, techniques=(): {
            "keys": list(keys),
            "techniques": list(techniques),
            "concluded": [],
            "in_flight": [],
        },
    )

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("the coverage route must not execute a workflow")

    monkeypatch.setattr(WorkflowsService, "execute_workflow", _forbidden)

    app = FastAPI()
    app.include_router(router.router, prefix=router.ROUTER_META.prefix)
    return TestClient(app)


def test_plain_text_report_is_uncovered_with_a_proposal(client):
    response = client.post(
        PATH,
        json={
            "report": "Phishing infrastructure at 203.0.113.7 delivering T1566 lures"
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "uncovered"
    assert result["keys"] == [IP]
    assert result["techniques"] == ["T1566"]
    assert result["proposal"]["hypothesis_subjects"] == {
        result["proposal"]["hypothesis"]: [IP]
    }


def test_an_empty_ask_is_a_400(client):
    response = client.post(PATH, json={"report": "   "})

    assert response.status_code == 400
    assert "nothing to check" in response.json()["detail"]


def test_a_malformed_body_is_refused_before_the_classifier(client):
    response = client.post(PATH, json={"entity_keys": "ip:203.0.113.7"})

    assert response.status_code == 422
