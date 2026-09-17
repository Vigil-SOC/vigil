"""The MCP surface is closed on a fresh install, and says who reached it.

Three answers, and the difference between them is the point: a closed surface
is not found, an unauthenticated caller is challenged, and an authenticated one
acts as the person its credential belongs to.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

os.environ.setdefault("VIGIL_CSRF_ENABLED", "false")


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from services.api.main import app

    with TestClient(app) as c:
        yield c


def test_a_fresh_install_does_not_serve_it():
    """The setting an install ships with, before any operator touches it."""
    from core.config import Settings

    assert Settings().vigil_mcp_enabled is False


def test_a_closed_surface_is_not_found_rather_than_forbidden(client):
    """A door nobody opened should not announce that it exists and is locked."""
    with patch("services.api.mcp_surface.is_enabled", return_value=False):
        response = client.post("/mcp", json={})

    assert response.status_code == 404


def test_an_open_surface_challenges_a_caller_with_no_credential(client):
    with patch("services.api.mcp_surface.is_enabled", return_value=True):
        response = client.post("/mcp", json={})

    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_a_credential_that_does_not_work_reads_the_same_as_none(client):
    with patch("services.api.mcp_surface.is_enabled", return_value=True):
        none_given = client.post("/mcp", json={})
        one_given = client.post(
            "/mcp", json={}, headers={"Authorization": "Bearer vgl_mcp_nothing"}
        )

    assert none_given.status_code == one_given.status_code == 401
    assert none_given.json() == one_given.json()


def test_a_session_token_does_not_open_the_mcp_surface(client):
    """The refusal runs both ways: this is not where a session belongs."""
    a_jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.signature"

    with patch("services.api.mcp_surface.is_enabled", return_value=True):
        response = client.post(
            "/mcp", json={}, headers={"Authorization": f"Bearer {a_jwt}"}
        )

    assert response.status_code == 401


def test_the_caller_is_bound_for_the_length_of_the_call():
    """What `caller()` reads, so a tool records who called rather than a claim."""
    from core.integrations.mcp.surface import acting_as, current_caller
    from tools.mcp.vigil import caller

    assert current_caller() is None
    assert caller() == "agent"

    with acting_as("nestor"):
        assert caller() == "nestor"

    assert caller() == "agent"


# --- The development credential ---------------------------------------------
#
# A fixed token so the surface can be tried locally without minting one first.
# It is not a secret, so what matters is that it works nowhere but behind the
# bypass, and that its use is not silent.


def test_the_development_credential_is_refused_when_the_bypass_is_off(client):
    from services.api.mcp_surface import DEV_MODE_TOKEN

    with patch("services.api.mcp_surface.is_enabled", return_value=True), patch(
        "core.config.get_settings"
    ) as settings:
        settings.return_value.dev_mode = False
        response = client.post(
            "/mcp", json={}, headers={"Authorization": f"Bearer {DEV_MODE_TOKEN}"}
        )

    assert response.status_code == 401


def test_the_development_credential_is_not_a_secret():
    """A constant in the source, written so nobody mistakes it for one."""
    from services.api.mcp_surface import DEV_MODE_TOKEN

    assert "DEV_MODE_ONLY" in DEV_MODE_TOKEN
    assert "not_a_secret" in DEV_MODE_TOKEN


def test_the_development_credential_is_not_one_the_service_would_accept():
    """It lives at the edge; nothing was minted, so authenticate() refuses it."""
    from core.auth.mcp_credential_service import authenticate
    from services.api.mcp_surface import DEV_MODE_TOKEN

    assert authenticate(DEV_MODE_TOKEN) is None


def test_using_the_development_credential_is_announced(caplog):
    """Loud, like the other gates the bypass opens."""
    import logging

    from services.api.mcp_surface import DEV_MODE_TOKEN, _dev_mode_user

    with patch("core.config.get_settings") as settings:
        settings.return_value.dev_mode = False
        with caplog.at_level(logging.WARNING):
            assert _dev_mode_user(DEV_MODE_TOKEN) is None

    assert "bypass is" in caplog.text


def test_another_token_is_not_the_development_credential():
    from services.api.mcp_surface import _dev_mode_user

    assert _dev_mode_user("vgl_mcp_something_else") is None
