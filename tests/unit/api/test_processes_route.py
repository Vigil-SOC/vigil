"""GET /api/processes is signed-in only, and /api/health says nothing about processes."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-for-prod")

pytestmark = pytest.mark.unit

STATES = {"agent_layer": "no_token", "daemon": "not_running"}


class _Fake:
    async def read(self):
        return dict(STATES)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("TESTING", "true")
    from core.config import get_settings
    from core.deps import provide_process_health
    from services.api.main import app

    get_settings.cache_clear()
    app.dependency_overrides[provide_process_health] = lambda: _Fake()
    yield app
    app.dependency_overrides.pop(provide_process_health, None)


def test_refused_without_a_sign_in(app):
    assert TestClient(app).get("/api/processes").status_code == 401


def test_answers_a_signed_in_user(app):
    from services.api.middleware.auth import get_current_active_user

    app.dependency_overrides[get_current_active_user] = lambda: SimpleNamespace(
        user_id="u-1"
    )
    try:
        response = TestClient(app).get("/api/processes")
    finally:
        app.dependency_overrides.pop(get_current_active_user, None)
    assert response.status_code == 200
    assert response.json() == STATES


def test_the_public_health_route_does_not_report_processes(app):
    assert "processes" not in TestClient(app).get("/api/health").json()
