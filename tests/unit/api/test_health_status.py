"""GET /api/health and /api/health/ready: degraded when storage is down, never leaking why."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit

_LEAK = "Missing tables: sla_policies"


class _Service:
    def __init__(self, info):
        self._info = info

    def get_backend_info(self):
        return self._info

    def is_using_database(self):
        return bool(self._info.get("database_available"))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TESTING", "true")
    from core.config import get_settings
    from services.api.main import app

    get_settings.cache_clear()
    monkeypatch.setattr("core.storage.connection.init_database", lambda *a, **k: None)
    monkeypatch.setattr(
        "core.storage.connection.get_schema_drift_report",
        lambda: None,
    )
    monkeypatch.setattr(
        "core.storage.database_data_service.DatabaseDataService",
        lambda *a, **k: _Service(
            {"backend": "none", "database_available": False, "demo_mode": False}
        ),
    )

    with TestClient(app) as test_client:
        yield test_client


def _backend(monkeypatch, info):
    from services.api.main import app

    monkeypatch.setattr(app.state, "health_storage", _Service(info), raising=False)


def _boom(monkeypatch, error):
    from services.api.main import app

    class Boom:
        def get_backend_info(self):
            raise error

    monkeypatch.setattr(app.state, "health_storage", Boom(), raising=False)


def test_unavailable_database_is_degraded_outside_demo_mode(client, monkeypatch):
    monkeypatch.setattr("core.config.is_demo_mode", lambda: False)
    _backend(
        monkeypatch,
        {"backend": "none", "database_available": False, "demo_mode": False},
    )

    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["demo_mode"] is False
    assert body["storage"]["database_available"] is False


def test_demo_mode_without_a_database_stays_healthy(client, monkeypatch):
    monkeypatch.setattr("core.config.is_demo_mode", lambda: True)
    _backend(
        monkeypatch,
        {"backend": "demo", "database_available": False, "demo_mode": True},
    )

    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["demo_mode"] is True
    assert body["storage"]["database_available"] is False


def test_connected_database_stays_healthy_when_schema_is_not_ok(client, monkeypatch):
    monkeypatch.setattr("core.config.is_demo_mode", lambda: False)
    monkeypatch.setattr(
        "core.storage.connection.get_schema_drift_report",
        lambda: {
            "state": "drifted",
            "missing_tables": ["sla_policies"],
            "missing_columns": {"cases": ["priority"]},
        },
    )
    _backend(
        monkeypatch,
        {"backend": "postgresql", "database_available": True, "demo_mode": False},
    )

    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["schema"] == {"state": "drifted"}
    assert "sla_policies" not in response.text
    assert "priority" not in response.text


def test_storage_check_failure_is_degraded_and_does_not_leak(
    client, monkeypatch, caplog
):
    from core.storage.connection import SchemaDriftError

    monkeypatch.setenv("DEV_MODE", "true")
    from core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr("core.config.is_demo_mode", lambda: True)
    monkeypatch.setattr(
        "core.storage.connection.get_schema_drift_report",
        lambda: {
            "state": "drifted",
            "missing_tables": ["sla_policies"],
            "missing_columns": {"cases": ["priority"]},
        },
    )

    _boom(monkeypatch, SchemaDriftError(_LEAK))

    with caplog.at_level(logging.ERROR, logger="services.api.main"):
        response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["demo_mode"] is True
    assert body["auth_bypassed"] is True
    assert body["schema"] == {"state": "drifted"}
    assert body["storage"] == {"backend": "unknown", "error": "storage_check_failed"}
    assert _LEAK not in response.text
    assert "sla_policies" not in response.text
    assert "priority" not in response.text
    assert _LEAK in caplog.text


def test_ready_is_503_when_degraded_and_200_otherwise(client, monkeypatch):
    monkeypatch.setattr("core.config.is_demo_mode", lambda: False)
    _backend(
        monkeypatch,
        {"backend": "none", "database_available": False, "demo_mode": False},
    )
    down = client.get("/api/health/ready")
    assert down.status_code == 503
    assert down.json() == client.get("/api/health").json()

    _backend(
        monkeypatch,
        {"backend": "postgresql", "database_available": True, "demo_mode": False},
    )
    assert client.get("/api/health/ready").status_code == 200

    monkeypatch.setattr("core.config.is_demo_mode", lambda: True)
    _backend(
        monkeypatch,
        {"backend": "demo", "database_available": False, "demo_mode": True},
    )
    assert client.get("/api/health/ready").status_code == 200


def test_ready_503_on_storage_check_failure_does_not_leak(client, monkeypatch):
    _boom(monkeypatch, RuntimeError(_LEAK))
    response = client.get("/api/health/ready")
    assert response.status_code == 503
    assert response.json()["storage"]["error"] == "storage_check_failed"
    assert _LEAK not in response.text
