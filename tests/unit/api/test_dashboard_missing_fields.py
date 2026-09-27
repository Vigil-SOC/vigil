"""The dashboard's summary and timeline over findings a source sent without a
severity or a timestamp. Both are nullable (LogLM parquet ingest leaves severity
unset, and a row without event_start_time has no timestamp); each used to fail
the whole endpoint with a 500.

Runs on the throwaway database tests/unit/conftest.py provisions per process.
"""

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.storage.models import Finding
from core.storage.service import DatabaseService
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


def _delete_test_findings():
    with unit_of_work() as session:
        session.query(Finding).filter(Finding.finding_id.like("mf-%")).delete(
            synchronize_session=False
        )


@pytest.fixture(autouse=True)
def _clean():
    _delete_test_findings()
    yield
    _delete_test_findings()


def _create(finding_id, *, timestamp=None, severity=None):
    DatabaseService().create_finding(
        finding_id=finding_id,
        mitre_predictions={},
        anomaly_score=None,
        timestamp=timestamp,
        data_source="loglm",
        severity=severity,
        entity_context={"src_ip": "192.0.2.77"},
    )


@pytest.fixture
def client(authenticate_app):
    from core.api.v1.findings_router import router as findings_router
    from services.api.routers.timeline import router as timeline_router

    app = FastAPI()
    app.include_router(findings_router, prefix="/api/findings")
    app.include_router(timeline_router, prefix="/api/timeline")
    authenticate_app(app)
    return TestClient(app)


def test_the_summary_counts_a_finding_with_no_severity_as_unknown(client):
    _create("mf-unrated", timestamp=utcnow())
    response = client.get("/api/findings/stats/summary")
    assert response.status_code == 200, response.text
    assert response.json()["by_severity"].get("unknown", 0) >= 1


def test_the_timeline_range_places_dated_findings_past_a_page_of_undated_ones(client):
    """timestamp DESC puts NULLs first in Postgres. Skipping undated rows after
    the query would stop the 500 but leave a full page of them filling the
    limit, so the dashboard's timeline would still come back empty."""
    for i in range(5):
        _create(f"mf-undated-{i}")
    # Far future, so no dated row another test left behind outranks it.
    _create("mf-dated", timestamp=datetime(2100, 1, 1), severity="high")

    response = client.get("/api/timeline/range", params={"limit": 5})
    assert response.status_code == 200, response.text
    ids = {e.get("metadata", {}).get("finding_id") for e in response.json()["events"]}
    assert "mf-dated" in ids
    assert not any(i and i.startswith("mf-undated") for i in ids)
