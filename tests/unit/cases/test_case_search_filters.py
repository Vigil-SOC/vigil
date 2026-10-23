from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.storage.models import Case
from core.storage.service import DatabaseService
from core.storage.unit_of_work import unit_of_work

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


def _delete_test_cases():
    with unit_of_work() as session:
        session.query(Case).filter(Case.case_id.like("cs-%")).delete(
            synchronize_session=False
        )


@pytest.fixture(autouse=True)
def _clean():
    _delete_test_cases()
    yield
    _delete_test_cases()


def _case(case_id, created_at, **fields):
    defaults = {
        "description": "beacon to rare domain",
        "status": "open",
        "priority": "high",
        "assignee": "analyst",
        "tags": ["c2", "dns"],
        "mitre_techniques": ["T1071"],
    }
    DatabaseService().create_case(case_id, case_id, [], **{**defaults, **fields})
    with unit_of_work() as session:
        session.get(Case, case_id).created_at = created_at


@pytest.fixture
def client(authenticate_app):
    from core.api.v1.cases_router import router

    app = FastAPI()
    app.include_router(router, prefix="/api/cases")
    authenticate_app(app)
    return TestClient(app)


def _search(client, **body):
    response = client.post("/api/cases/search", json={**body, "limit": 50, "offset": 0})
    assert response.status_code == 200, response.text
    return sorted(c["case_id"] for c in response.json()["results"])


def test_the_console_body_matches_on_every_field(client):
    _case("cs-match", datetime(2026, 6, 30, 23, 30))
    _case("cs-next-day", datetime(2026, 7, 1, 0, 30))
    _case("cs-before", datetime(2026, 5, 31, 23, 0))
    _case("cs-other-owner", datetime(2026, 6, 10), assignee="someone")
    _case("cs-one-tag", datetime(2026, 6, 10), tags=["c2"])
    _case("cs-other-text", datetime(2026, 6, 10), description="phishing")

    assert _search(
        client,
        query_text="beacon",
        priority=["high"],
        status=["open"],
        assignee=["analyst"],
        tags=["c2", "dns"],
        created_after="2026-06-01",
        created_before="2026-06-30T23:59:59",
    ) == ["cs-match"]


def test_mitre_techniques_filter_matches_the_array(client):
    _case("cs-t1071", datetime(2026, 6, 10))
    _case("cs-t1059", datetime(2026, 6, 10), mitre_techniques=["T1059"])

    assert _search(client, mitre_techniques=["T1059"]) == ["cs-t1059"]
