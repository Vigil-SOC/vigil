"""
API-contract integration tests for auth endpoints.

These tests replace tests/broken_tests/integration/test_auth_api.py, which
expected fixtures (test_client, sample_user, sample_role, auth_headers)
that never existed. Full DB-backed fixtures are out of scope for this
pass — see tests/integration/conftest.py TODO.

What's covered here: endpoint wiring + contract (what each endpoint
accepts and the shape of the errors it returns). Behaviors that require
a persisted user (lockout, history, reset flow end-to-end) are validated
via unit tests in tests/unit/ against the underlying services.
"""

import os

# DEV_MODE bypasses the real DB for endpoints that resolve current_user.
os.environ.setdefault("DEV_MODE", "true")
# Deterministic secret so JWT issuance during tests is reproducible.
os.environ.setdefault(
    "JWT_SECRET_KEY", "integration-test-secret-not-for-production-use-12345"
)
# Keep CSRF out of the way for these contract checks; CSRF is exercised
# by frontend Playwright / manual validation.
os.environ.setdefault("VIGIL_CSRF_ENABLED", "false")

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from backend.main import app

    with TestClient(app) as c:
        yield c


@pytest.mark.integration
class TestRemovedEndpoints:
    def test_register_returns_404(self, client):
        """Public self-registration was removed in PR 1. Attempting to
        hit the old endpoint must 404 so scanners / clients notice."""
        resp = client.post(
            "/api/auth/register",
            json={
                "username": "x",
                "email": "x@example.com",
                "password": "Password1234!",
                "full_name": "x",
            },
        )
        assert resp.status_code == 404


@pytest.mark.integration
class TestLoginContract:
    def test_login_rejects_missing_fields(self, client):
        resp = client.post("/api/auth/login", json={})
        assert resp.status_code == 422

    def test_login_rejects_unknown_user(self, client):
        resp = client.post(
            "/api/auth/login",
            json={
                "username_or_email": "no-such-user-integration-test",
                "password": "Password12345!",
            },
        )
        # Unknown user yields 401. Rate limiter may flip to 429 if this
        # test runs many times in a single process; accept both.
        assert resp.status_code in (401, 429)


@pytest.mark.integration
class TestPasswordResetContract:
    def test_request_always_returns_200_for_unknown_email(self, client):
        """No user-enumeration via response shape."""
        resp = client.post(
            "/api/auth/password-reset/request",
            json={"email": "never-registered-integration-test@example.com"},
        )
        assert resp.status_code in (200, 429)
        if resp.status_code == 200:
            assert "reset link" in resp.json()["message"].lower()

    def test_request_rejects_invalid_email(self, client):
        resp = client.post(
            "/api/auth/password-reset/request",
            json={"email": "not-an-email"},
        )
        assert resp.status_code == 422

    def test_confirm_rejects_bad_token(self, client):
        resp = client.post(
            "/api/auth/password-reset/confirm",
            json={"token": "garbage-token", "new_password": "ANewGoodPass123!"},
        )
        assert resp.status_code in (400, 429)


@pytest.mark.integration
class TestSecurityHeaders:
    """Smoke test that the middlewares from PR 2 + PR 4 are actually
    wired into the app."""

    def test_response_includes_security_headers(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        # Set by SecurityHeadersMiddleware in all envs.
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
