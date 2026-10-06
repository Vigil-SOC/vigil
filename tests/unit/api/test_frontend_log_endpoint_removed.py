"""The unused /api/logs/frontend sink was removed (#1568): it let any user forge log lines."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-for-prod")
os.environ.setdefault("DEV_MODE", "true")

pytestmark = pytest.mark.unit


def test_frontend_log_routes_are_gone_for_authenticated_users():
    from services.api.main import app
    from services.api.middleware.auth import get_current_active_user

    class _User:
        is_active = True
        username = "t"
        user_id = "t"

    app.dependency_overrides[get_current_active_user] = lambda: _User()
    try:
        client = TestClient(app)
        entry = {"level": "error", "component": "c", "message": "m\nforged"}
        assert client.post("/api/logs/frontend", json=entry).status_code == 404
        assert client.get("/api/logs/frontend/status").status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_active_user, None)
