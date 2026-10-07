"""Noise mark and intake launch on the unversioned findings router."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.findings.overview import overview_payload
from core.storage.models import Finding, IntakeTrigger
from core.storage.unit_of_work import unit_of_work
from services.api.middleware.auth import get_current_user

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

AT = datetime(2999, 3, 1, 12, 0, 0)


class _User:
    user_id = "user-ov"


@pytest.fixture(autouse=True)
def _clean(throwaway_database):
    def purge():
        with unit_of_work() as session:
            session.query(IntakeTrigger).filter(
                IntakeTrigger.finding_id.like("ov-act-%")
            ).delete(synchronize_session=False)
            session.query(Finding).filter(Finding.finding_id.like("ov-act-%")).delete(
                synchronize_session=False
            )

    purge()
    yield
    purge()


@pytest.fixture
def client():
    from services.api.routers import findings

    app = FastAPI()
    app.dependency_overrides[get_current_user] = lambda: _User()
    app.include_router(findings.router, prefix="/api/findings")
    return TestClient(app)


def _ids(day: datetime) -> list[str]:
    payload = overview_payload(day=day.date(), now=day)
    return [row["finding_id"] for row in payload["feed"]]


def test_mark_then_clear_hides_and_restores_the_feed_row(client):
    with unit_of_work() as session:
        session.add(
            Finding(
                finding_id="ov-act-noise",
                data_source="ov-src",
                created_at=AT,
                status="new",
                severity="high",
            )
        )

    marked = client.post("/api/findings/ov-act-noise/noise")
    assert marked.status_code == 200
    assert marked.json()["noise_marked_by"] == "user-ov"
    assert marked.json()["status"] == "new"
    assert marked.json()["noise_marked_at"]
    assert "ov-act-noise" not in _ids(AT)

    cleared = client.delete("/api/findings/ov-act-noise/noise")
    assert cleared.status_code == 200
    assert cleared.json()["noise_marked_at"] is None
    assert cleared.json()["noise_marked_by"] is None
    assert cleared.json()["status"] == "new"
    assert "ov-act-noise" in _ids(AT)


def test_second_launch_does_not_insert_another_queued_row(client):
    with unit_of_work() as session:
        session.add(
            Finding(
                finding_id="ov-act-launch",
                data_source="ov-src",
                created_at=AT,
                status="new",
                severity="high",
            )
        )

    first = client.post("/api/findings/ov-act-launch/intake")
    assert first.status_code == 200
    assert first.json()["queued"] is True
    assert first.json()["already_queued"] is False

    second = client.post("/api/findings/ov-act-launch/intake")
    assert second.status_code == 200
    assert second.json()["queued"] is False
    assert second.json()["already_queued"] is True
    assert second.json()["trigger_id"] is None

    with unit_of_work() as session:
        rows = (
            session.query(IntakeTrigger)
            .filter(IntakeTrigger.finding_id == "ov-act-launch")
            .all()
        )
        assert len(rows) == 1
        assert rows[0].kind == "detection"
        assert rows[0].state == "queued"
        assert rows[0].priority == "high"
        finding = session.get(Finding, "ov-act-launch")
        assert finding is not None
        assert finding.status == "new"


def test_missing_finding_is_not_marked_or_launched(client):
    assert client.post("/api/findings/ov-act-missing/noise").status_code == 404
    assert client.post("/api/findings/ov-act-missing/intake").status_code == 404
    with unit_of_work() as session:
        assert (
            session.query(IntakeTrigger)
            .filter(IntakeTrigger.finding_id == "ov-act-missing")
            .count()
            == 0
        )
