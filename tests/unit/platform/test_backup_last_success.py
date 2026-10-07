"""Last successful backup time on /metrics and the storage status action."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.backup.status import read_last_success_at
from core.platform.monitoring import get_metrics_response
from services.api.routers import storage_status

pytestmark = pytest.mark.unit

_METRIC = "vigil_backup_last_success_timestamp_seconds"
_SETUP = "Set up automated backups for data protection"
WHEN = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _write_status(tmp_path, payload) -> None:
    (tmp_path / "backup_status.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload),
        encoding="utf-8",
    )


def _gauge(body: bytes) -> float | None:
    prefix = f"{_METRIC} ".encode()
    found = [
        float(line.split()[1]) for line in body.splitlines() if line.startswith(prefix)
    ]
    assert len(found) <= 1
    return found[0] if found else None


@pytest.fixture
def vigil_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_DIR", str(tmp_path))
    return tmp_path


def test_metrics_exports_the_unix_time(vigil_dir):
    _write_status(vigil_dir, {"last_success_at": WHEN.isoformat()})

    response = get_metrics_response()

    assert response.status_code == 200
    assert _gauge(response.body) == WHEN.timestamp()


def test_metrics_omits_the_sample_when_the_file_is_missing(vigil_dir):
    response = get_metrics_response()

    assert _METRIC.encode() not in response.body
    assert _gauge(response.body) is None


@pytest.mark.parametrize(
    "payload",
    [
        "{",
        {},
        {"last_success_at": None},
        {"last_success_at": "not-a-time"},
        {"last_success_at": "2026-10-01T12:00:00"},
        [],
    ],
)
def test_metrics_omits_the_sample_when_the_timestamp_does_not_parse(vigil_dir, payload):
    _write_status(vigil_dir, payload)

    assert _METRIC.encode() not in get_metrics_response().body
    assert read_last_success_at() is None


def _status_client(monkeypatch) -> TestClient:
    class _Service:
        def get_backend_info(self):
            return {"backend": "postgresql", "database_available": True}

    monkeypatch.setattr(
        "core.storage.database_data_service.DatabaseDataService",
        _Service,
    )
    app = FastAPI()
    app.include_router(storage_status.router, prefix=storage_status.ROUTER_META.prefix)
    return TestClient(app)


def test_storage_status_names_the_timestamp(vigil_dir, monkeypatch):
    _write_status(vigil_dir, {"last_success_at": WHEN.isoformat()})

    response = _status_client(monkeypatch).get("/api/storage/status")

    assert response.status_code == 200
    body = response.json()
    assert body["recommendations"] == [
        {
            "title": "Database Running",
            "description": "PostgreSQL is active and ready for production use.",
            "action": f"Last successful backup at {WHEN.isoformat()}",
            "priority": "low",
        }
    ]


def test_storage_status_keeps_the_setup_action_without_a_file(vigil_dir, monkeypatch):
    response = _status_client(monkeypatch).get("/api/storage/status")

    assert response.status_code == 200
    body = response.json()
    assert body["recommendations"][0]["title"] == "Database Running"
    assert body["recommendations"][0]["action"] == _SETUP
