# POST /workflows/threat-hunt/coverage is the same function as the agent tool,
# and it never reaches execute_workflow (#903).

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.memory import hunt_claim, hunt_coverage
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


# The proposal speaks the report's own claim (#1918).

BRIEF = """# Brief

Activity from 203.0.113.7 and T1621.

## Hunting hypothesis

An MFA approval follows a burst of denials, a key is created on a service user,
then the bucket is read from a new IP.

## Indicators
- 203.0.113.7
"""
CLAIM = (
    "An MFA approval follows a burst of denials, a key is created on a service "
    "user, then the bucket is read from a new IP."
)


def _running(monkeypatch):
    row = {
        "run_id": "wfr-live",
        "case_id": "case-1",
        "status": "running",
        "started_at": "2026-09-01T00:00:00Z",
        "hypothesis": "x",
        "matched_keys": [IP],
        "matched_techniques": [],
    }
    monkeypatch.setattr(
        hunt_coverage,
        "list_prior_hunts",
        lambda keys, techniques=(): {
            "keys": list(keys),
            "techniques": list(techniques),
            "concluded": [],
            "in_flight": [row],
        },
    )


def test_a_stated_hypothesis_is_proposed_verbatim_with_the_indicators_as_subjects(
    client,
):
    result = client.post(PATH, json={"report": BRIEF}).json()

    assert result["proposal"]["hypothesis"] == CLAIM
    assert result["proposal"]["hypothesis_subjects"] == {CLAIM: [IP]}


def test_without_one_the_summarization_model_writes_the_claim(client, monkeypatch):
    async def model(_report):
        return "203.0.113.7 beacons from the web tier."

    monkeypatch.setattr(hunt_claim, "model_claim", model)

    result = client.post(PATH, json={"report": "C2 at 203.0.113.7"}).json()

    assert result["proposal"]["hypothesis"] == "203.0.113.7 beacons from the web tier."


def test_a_failed_model_call_keeps_the_indicator_template(client, monkeypatch):
    async def model(_report):
        return None

    monkeypatch.setattr(hunt_claim, "model_claim", model)

    result = client.post(PATH, json={"report": "C2 at 203.0.113.7"}).json()

    assert result["proposal"]["hypothesis"].startswith("Activity from the reported")


def test_a_repeat_document_is_running_and_still_gets_a_proposal(client, monkeypatch):
    _running(monkeypatch)

    result = client.post(PATH, json={"report": BRIEF}).json()

    assert result["status"] == "running"
    assert result["in_flight"][0]["case_id"] == "case-1"
    assert result["proposal"]["hypothesis"] == CLAIM
