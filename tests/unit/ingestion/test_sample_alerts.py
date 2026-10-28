"""/api/ingest/sample-alerts against Postgres: load, reload, clear, demo mode,
and that the daemon's backfill sees loaded samples as untriaged."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from core.ingestion.sample_alerts import (
    SAMPLE_COUNT,
    SAMPLE_DATA_SOURCE,
    build_sample_alerts,
    sample_finding_id,
)
from core.storage.models import Investigation
from core.storage.unit_of_work import unit_of_work

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

REAL = "test-sample-real-1"


def _purge():
    with unit_of_work() as s:
        s.execute(
            text("DELETE FROM investigations WHERE investigation_id LIKE 'test-sample-%'")
        )
        s.execute(text("DELETE FROM cases WHERE case_id LIKE 'test-sample-%'"))
        s.execute(
            text("DELETE FROM findings WHERE data_source = :src OR finding_id = :real"),
            {"src": SAMPLE_DATA_SOURCE, "real": REAL},
        )


@pytest.fixture
def db(throwaway_database):
    from core.storage.service import DatabaseService

    _purge()
    yield DatabaseService()
    _purge()


@pytest.fixture
def client(db, monkeypatch):
    from core.auth.auth_service import AuthService
    from core.auth.current_user import get_current_user
    from services.api.routers import ingestion

    monkeypatch.setattr(ingestion, "is_demo_mode", lambda: False)
    monkeypatch.setattr(AuthService, "check_permission", lambda *_a, **_k: True)
    app = FastAPI()
    app.include_router(ingestion.router, prefix=ingestion.ROUTER_META.prefix)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        user_id="u-1"
    )
    return TestClient(app)


def test_the_set_is_marked_and_spans_every_severity():
    rows = build_sample_alerts(now=datetime(2026, 1, 1, 12, 0))
    assert len(rows) == SAMPLE_COUNT
    assert {r["data_source"] for r in rows} == {SAMPLE_DATA_SOURCE}
    assert {r["severity"] for r in rows} == {"critical", "high", "medium", "low"}
    assert len({r["finding_id"] for r in rows}) == SAMPLE_COUNT
    assert all(r["timestamp"] < datetime(2026, 1, 1, 12, 0) for r in rows)


def test_load_stores_the_set_once(client, db):
    assert client.get("/api/ingest/sample-alerts").json() == {
        "loaded": 0,
        "total": SAMPLE_COUNT,
    }
    first = client.post("/api/ingest/sample-alerts")
    assert first.status_code == 200
    assert first.json() == {"loaded": SAMPLE_COUNT, "total": SAMPLE_COUNT}
    again = client.post("/api/ingest/sample-alerts")
    assert again.json()["loaded"] == SAMPLE_COUNT

    stored = db.get_finding(sample_finding_id("encoded-powershell"))
    assert stored.data_source == SAMPLE_DATA_SOURCE
    assert stored.severity == "critical"


def test_loaded_samples_wait_for_the_daemon_backfill(client, db):
    client.post("/api/ingest/sample-alerts")
    waiting = db.get_findings_missing_enrichment(
        limit=100, max_age_hours=24, exclusions="hide"
    )
    ids = {f["finding_id"] for f in waiting}
    assert {sample_finding_id(r["external_id"]) for r in build_sample_alerts()} <= ids


def test_clear_removes_samples_and_their_own_cases_only(client, db):
    client.post("/api/ingest/sample-alerts")
    db.create_finding(
        finding_id=REAL,
        mitre_predictions={},
        anomaly_score=0.5,
        timestamp=None,
        data_source="splunk",
        title="A real alert",
    )
    only_samples = [sample_finding_id("phish-click"), sample_finding_id("inbox-rule")]
    db.create_case("test-sample-case-a", "Sample story", only_samples)
    db.create_case(
        "test-sample-case-b", "Mixed", [sample_finding_id("vuln-scan"), REAL]
    )

    res = client.delete("/api/ingest/sample-alerts")
    assert res.status_code == 200
    assert res.json() == {"findings_deleted": SAMPLE_COUNT, "cases_deleted": 1}

    assert client.get("/api/ingest/sample-alerts").json()["loaded"] == 0
    assert db.get_case("test-sample-case-a") is None
    kept = db.get_case("test-sample-case-b")
    assert [f.finding_id for f in kept.findings] == [REAL]
    assert db.get_finding(REAL) is not None


def test_clear_refuses_while_a_sample_case_is_being_investigated(client, db):
    client.post("/api/ingest/sample-alerts")
    db.create_case("test-sample-case-live", "Sample story", [sample_finding_id("vuln-scan")])
    with unit_of_work() as s:
        s.add(
            Investigation(
                investigation_id="test-sample-inv-live",
                case_id="test-sample-case-live",
                workflow_id="incident-response",
                trigger_type="finding",
                status="executing",
                workdir="/tmp/inv",
            )
        )

    res = client.delete("/api/ingest/sample-alerts")
    assert res.status_code == 409
    assert "test-sample-inv-live" in res.json()["detail"]
    assert db.get_case("test-sample-case-live") is not None
    assert client.get("/api/ingest/sample-alerts").json()["loaded"] == SAMPLE_COUNT


def test_demo_mode_refuses(client, monkeypatch):
    from services.api.routers import ingestion

    monkeypatch.setattr(ingestion, "is_demo_mode", lambda: True)
    res = client.post("/api/ingest/sample-alerts")
    assert res.status_code == 409
    assert "demo mode" in res.json()["detail"].lower()
