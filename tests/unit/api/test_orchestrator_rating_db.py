"""Findings waiting to be rated, and never rated, against the throwaway Postgres."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from core.time import utcnow
from services.api.routers.orchestrator import router as orchestrator_router

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

RATED = {"ai_triage": {"severity": "low"}}
TRIAGE_FAILED = {"ai_triage_error": "gateway down"}


@pytest.fixture(autouse=True)
def clean_findings():
    from core.storage.connection import get_db_manager
    from core.storage.models import Finding

    def clean():
        with get_db_manager().session_scope() as session:
            session.query(Finding).filter(Finding.finding_id.like("rating-%")).delete(
                synchronize_session=False
            )

    clean()
    yield
    clean()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(
        "services.api.routers.orchestrator._get_orchestrator", lambda: None
    )
    monkeypatch.setattr(
        "services.api.routers.orchestrator.get_settings",
        lambda: SimpleNamespace(daemon_enrich_backfill_max_age_hours=168),
    )
    app = FastAPI()
    app.include_router(orchestrator_router, prefix="/api/orchestrator")
    return TestClient(app)


def _store(name, *, stored_ago, enrichment=None, event_ago=timedelta(0)):
    from core.storage.connection import get_db_manager
    from core.storage.models import Finding

    now = utcnow()
    # Unset when unrated, as ingestion leaves it: an explicit None is JSON null.
    fields = {"ai_enrichment": enrichment} if enrichment is not None else {}
    with get_db_manager().session_scope() as session:
        session.add(
            Finding(
                finding_id=f"rating-{name}",
                data_source="test",
                **fields,
                created_at=now - stored_ago,
                timestamp=None if event_ago is None else now - event_ago,
            )
        )


def _sweep():
    from core.storage.service import DatabaseService

    batch = DatabaseService().get_findings_missing_enrichment(
        limit=50, max_age_hours=168
    )
    return [f["finding_id"] for f in batch if f["finding_id"].startswith("rating-")]


def test_status_counts_findings_waiting_to_be_rated_and_never_rated(client):
    for age in (timedelta(minutes=5), timedelta(hours=1), timedelta(days=8)):
        _store(f"rated-{age}", stored_ago=age, enrichment=RATED)
    _store("fresh", stored_ago=timedelta(minutes=5))
    _store("waiting", stored_ago=timedelta(hours=1), enrichment=TRIAGE_FAILED)
    _store("given-up", stored_ago=timedelta(days=8))

    body = client.get("/api/orchestrator/status").json()

    assert (body["waiting_to_be_rated"], body["never_rated"]) == (1, 1)


def test_age_is_when_it_was_stored_not_its_event_time(client):
    # Stored half an hour ago: one with no event time, one whose event is old.
    _store("no-event-time", stored_ago=timedelta(minutes=31), event_ago=None)
    _store("old-event", stored_ago=timedelta(minutes=30), event_ago=timedelta(days=30))
    _store("given-up", stored_ago=timedelta(days=8), event_ago=timedelta(minutes=1))

    body = client.get("/api/orchestrator/status").json()

    assert (body["waiting_to_be_rated"], body["never_rated"]) == (2, 1)
    assert _sweep() == ["rating-no-event-time", "rating-old-event"]


def test_a_failed_count_fails_the_status_instead_of_reporting_zero(client, monkeypatch):
    def unreachable(*a, **k):
        raise RuntimeError("findings unreadable")

    monkeypatch.setattr("core.storage.rating.count_unrated", unreachable)

    resp = client.get("/api/orchestrator/status")

    assert resp.status_code == 500
    assert resp.json() == {"detail": "findings unreadable"}


def test_the_counts_and_the_sweep_read_the_partial_index(client):
    from core.storage.connection import get_db_manager

    engine = get_db_manager().engine
    seen = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "ai_enrichment IS NULL" in statement:
            seen.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        client.get("/api/orchestrator/status")
        _sweep()
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert len(seen) == 2
    for statement, parameters in seen:
        with engine.begin() as conn:
            # A near-empty table favours a seq scan; the index must be usable.
            conn.exec_driver_sql("SET LOCAL enable_seqscan = off")
            plan = conn.exec_driver_sql("EXPLAIN " + statement, parameters)
            assert "idx_finding_unrated_created_at" in " ".join(r[0] for r in plan)
