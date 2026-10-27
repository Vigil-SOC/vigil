"""GET /api/search over a real database: alerts, cases, workflows, agents and skills."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.storage.models import Case, Finding
from core.storage.models.finding import FindingMitrePrediction
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


@pytest.fixture
def client(throwaway_database):
    from core.deps import provide_workflows
    from core.workflows.workflows_service import WorkflowsService
    from services.api.routers import search as search_router

    search_router._corpus = (0.0, [])
    now = utcnow().replace(tzinfo=None)
    with unit_of_work() as s:
        s.add(
            Finding(
                finding_id="srch-f1",
                title="Exfiltration over C2 from qzv-host-77",
                description="863 KB out to an unknown host",
                data_source="zeek",
                severity="high",
                timestamp=now,
                entity_context={"hostnames": ["qzv-host-77"], "src_ips": ["10.9.8.7"]},
            )
        )
        # says nothing about exfiltration in its text; only its predicted technique does
        s.add(
            Finding(
                finding_id="srch-f2",
                title="Large one-way transfer from qzv-host-88",
                description="15 MB out in one session, 2 KB back",
                data_source="zeek",
                severity="high",
                timestamp=now,
            )
        )
        s.add(
            Case(
                case_id="srch-c1",
                title="Ransomware precursor on finance file server",
                description="vssadmin delete shadows seen before encryption",
                status="open",
                priority="critical",
            )
        )
        s.flush()
        s.add(
            FindingMitrePrediction(
                finding_id="srch-f2", technique_id="T1041", confidence=0.77
            )
        )
        # too unsure to search by
        s.add(
            FindingMitrePrediction(
                finding_id="srch-f2", technique_id="T1486", confidence=0.2
            )
        )

    app = FastAPI()
    app.include_router(search_router.router, prefix=search_router.ROUTER_META.prefix)
    app.dependency_overrides[provide_workflows] = lambda: WorkflowsService()
    yield TestClient(app)

    with unit_of_work() as s:
        s.query(FindingMitrePrediction).filter(
            FindingMitrePrediction.finding_id.like("srch-%")
        ).delete(synchronize_session=False)
        s.query(Finding).filter(Finding.finding_id.like("srch-%")).delete(
            synchronize_session=False
        )
        s.query(Case).filter(Case.case_id.like("srch-%")).delete(
            synchronize_session=False
        )
    search_router._corpus = (0.0, [])


def _ids(body, kind):
    return [hit["id"] for hit in body["groups"][kind]]


def test_plain_language_finds_the_alert_and_says_why(client):
    body = client.get("/api/search", params={"q": "data theft", "limit": 25}).json()
    alert = next(a for a in body["groups"]["alert"] if a["id"] == "srch-f1")
    assert alert["why"] == "about exfiltration"
    assert alert["route"] == {"screen": "overview", "search": "?alert=srch-f1"}
    assert body["method"] == "words and related security terms"


def test_an_alert_is_found_by_a_confident_predicted_technique_only(client):
    theft = client.get("/api/search", params={"q": "data theft", "limit": 25}).json()
    assert "srch-f2" in _ids(theft, "alert")
    ransom = client.get("/api/search", params={"q": "ransomware", "limit": 25}).json()
    assert "srch-f2" not in _ids(ransom, "alert")


def test_a_host_or_address_finds_its_alert(client):
    assert _ids(
        client.get("/api/search", params={"q": "qzv-host-77"}).json(), "alert"
    ) == ["srch-f1"]
    assert "srch-f1" in _ids(
        client.get("/api/search", params={"q": "10.9.8.7"}).json(), "alert"
    )


def test_a_case_is_found_by_its_words_and_opens_as_a_case(client):
    body = client.get("/api/search", params={"q": "shadow copies"}).json()
    case = next(c for c in body["groups"]["case"] if c["id"] == "srch-c1")
    assert case["route"] == {"case": "srch-c1"}
    assert case["subtitle"] == "critical · open"


def test_agents_skills_and_workflows_are_searched_too(client):
    body = client.get("/api/search", params={"q": "phishing triage"}).json()
    assert body["groups"]["skill"][0]["id"] == "phishing-triage"
    assert (
        body["groups"]["skill"][0]["route"]["search"]
        == "?tab=skills&skill=phishing-triage"
    )
    agents = client.get(
        "/api/search", params={"q": "triage agent", "kinds": "agent"}
    ).json()
    assert set(agents["groups"]) == {"agent"}
    assert agents["groups"]["agent"][0]["id"] == "triage"


def test_the_query_is_bounded(client):
    assert client.get("/api/search", params={"q": ""}).status_code == 422
    assert client.get("/api/search", params={"q": "x" * 301}).status_code == 422

