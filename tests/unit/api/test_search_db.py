"""GET /api/search answers the command bar from one call.

Runs on the throwaway database tests/unit/conftest.py provisions per process, so
the case and findings text searches it reuses really run.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import insert

from core.storage.models import Case, CaseComment, Finding, case_findings
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

PREFIX = "srch-"
WORKFLOWS = [
    {"id": "phishing-response", "name": "Phishing response", "description": "Mail"},
    {"id": "insider-threat", "name": "Insider threat", "description": "Data theft"},
]


@pytest.fixture(autouse=True)
def _seed():
    finding = {"data_source": "zeek", "severity": "high", "timestamp": utcnow()}
    with unit_of_work() as session:
        session.execute(
            insert(Finding),
            [
                {
                    **finding,
                    "finding_id": f"{PREFIX}f-host",
                    "title": "Beacon to rare domain",
                    "entity_context": {"hostnames": ["FIN-WS-114"]},
                },
                {**finding, "finding_id": f"{PREFIX}f-other", "description": "Logons"},
            ],
        )
        for case_id, title in (("loader", "Loader in mail"), ("host", "Finance hunt")):
            session.add(Case(case_id=f"{PREFIX}{case_id}", title=title, status="open"))
        session.flush()
        session.add(
            CaseComment(case_id=f"{PREFIX}loader", author="ada", content="loader again")
        )
        session.execute(
            insert(case_findings),
            [{"case_id": f"{PREFIX}host", "finding_id": f"{PREFIX}f-host"}],
        )
    yield
    with unit_of_work() as session:
        session.query(Case).filter(Case.case_id.like(f"{PREFIX}%")).delete(
            synchronize_session=False
        )
        session.query(Finding).filter(Finding.finding_id.like(f"{PREFIX}%")).delete(
            synchronize_session=False
        )


@pytest.fixture
def search(authenticate_app):
    from services.api.routers import search

    app = FastAPI()
    app.include_router(search.router, prefix=search.ROUTER_META.prefix)
    authenticate_app(app)
    app.state.workflows = SimpleNamespace(list_workflows=lambda: WORKFLOWS)
    client = TestClient(app)
    return lambda q: client.get("/api/search", params={"q": q}).json()


def _ids(body, kind):
    return [hit["id"] for hit in body[kind]]


def test_a_host_finds_its_alert_and_the_case_through_it(search):
    body = search("fin-ws-114")
    assert _ids(body, "alerts") == [f"{PREFIX}f-host"]
    assert body["alerts"][0]["title"] == "Beacon to rare domain"
    assert _ids(body, "cases") == [f"{PREFIX}host"]
    assert body["cases"][0]["match_type"] == "alert"


def test_a_case_matched_by_title_and_comment_is_listed_once(search):
    body = search("loader")
    assert _ids(body, "cases") == [f"{PREFIX}loader"]
    assert body["cases"][0]["match_type"] == "case"


def test_exact_ids_still_answer_and_short_queries_only_as_ids(search):
    assert search(f"{PREFIX}f-other")["finding_exact"]["id"] == f"{PREFIX}f-other"
    assert search(f"{PREFIX}host")["case_exact"]["title"] == "Finance hunt"
    short = search("lo")
    assert short["cases"] == short["alerts"] == short["skills"] == []


def test_workflows_agents_and_skills_match_every_term(search):
    assert _ids(search("data theft"), "workflows") == ["insider-threat"]
    body = search("phishing")
    assert _ids(body, "workflows") == ["phishing-response"]
    assert "phishing-triage" in _ids(body, "skills")
    assert "threat_hunter" in _ids(search("threat hunt"), "agents")


def test_a_kind_the_role_cannot_read_stays_empty(search):
    def allow(user_id, permission, session=None):
        return permission != "findings.read"

    with patch("core.auth.auth_service.AuthService.check_permission", allow):
        body = search("fin-ws-114")
    assert body["alerts"] == [] and body["finding_exact"] is None
    assert _ids(body, "cases") == [f"{PREFIX}host"]
