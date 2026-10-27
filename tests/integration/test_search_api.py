"""GET /api/search against Postgres: alerts, cases, workflows, agents, skills."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

pytestmark = [pytest.mark.integration, pytest.mark.database]


@pytest.fixture(autouse=True)
def _database():
    """Skip only when Postgres is unreachable; fail on anything else."""
    from core.storage.connection import get_db_manager, init_database

    manager = get_db_manager()
    try:
        manager.initialize()
        session = manager.get_session()
        try:
            session.execute(text("SELECT 1"))
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            f"requires a local PostgreSQL (docker compose up -d postgres): {exc}"
        )
    init_database()


@pytest.fixture(scope="module")
def client():
    from services.api.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _authenticated(client, authenticate_app):
    authenticate_app(client.app)


@pytest.fixture
def data():
    from core.config import get_settings
    from core.search.engine import VectorCache
    from core.storage.models import Finding
    from core.storage.models.finding import FindingMitrePrediction
    from core.storage.unit_of_work import unit_of_work
    from services.api.routers import search as search_router

    index = search_router._index
    # words only: no model in the test, whatever the settings name
    index.model = (get_settings().search_embed_model or "").strip()
    index.cache = VectorCache(None)
    index.corpus = (0.0, [])
    # newest, so they are inside the newest-alerts window on a full database
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with unit_of_work() as s:
        s.add(
            Finding(
                finding_id="test-search-f1",
                title="Exfiltration over C2 from qzv-host-77",
                description="863 KB out to an unknown host",
                data_source="zeek",
                severity="high",
                timestamp=now,
                entity_context={"hostnames": ["qzv-host-77"]},
            )
        )
        # says nothing about exfiltration in its text; only its predicted technique does
        s.add(
            Finding(
                finding_id="test-search-f2",
                title="Large one-way transfer from qzv-host-88",
                description="15 MB out in one session, 2 KB back",
                data_source="zeek",
                severity="high",
                timestamp=now,
                entity_context={"hostnames": ["qzv-host-88"]},
            )
        )
        s.flush()
        s.add(
            FindingMitrePrediction(
                finding_id="test-search-f2", technique_id="T1041", confidence=0.77
            )
        )
    yield
    with unit_of_work() as s:
        s.execute(
            text(
                "DELETE FROM findings"
                " WHERE finding_id IN ('test-search-f1', 'test-search-f2')"
            )
        )
    index.cache, index.corpus = None, (0.0, [])


def test_plain_language_finds_the_alert(client, data):
    body = client.get("/api/search", params={"q": "data theft", "limit": 25}).json()
    alert = next(a for a in body["groups"]["alert"] if a["id"] == "test-search-f1")
    assert alert["why"] == "about exfiltration"
    assert alert["route"] == {"screen": "overview", "search": "?alert=test-search-f1"}
    assert body["method"] == "words and related security terms"


def test_an_alert_is_found_by_its_predicted_technique(client, data):
    body = client.get("/api/search", params={"q": "data theft", "limit": 25}).json()
    alert = next(a for a in body["groups"]["alert"] if a["id"] == "test-search-f2")
    assert alert["why"] == "about exfiltration" and alert["by"] != "meaning"


def test_a_host_finds_its_alert(client, data):
    body = client.get("/api/search", params={"q": "qzv-host-77"}).json()
    assert [a["id"] for a in body["groups"]["alert"]] == ["test-search-f1"]


def test_agents_skills_and_workflows_are_searched_too(client, data):
    body = client.get("/api/search", params={"q": "phishing triage"}).json()
    assert body["groups"]["skill"][0]["id"] == "phishing-triage"
    assert (
        body["groups"]["skill"][0]["route"]["search"]
        == "?tab=skills&skill=phishing-triage"
    )
    agents = client.get(
        "/api/search", params={"q": "triage agent", "kinds": "agent"}
    ).json()
    assert (
        set(agents["groups"]) == {"agent"}
        and agents["groups"]["agent"][0]["id"] == "triage"
    )
