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


# --- Reachable from somewhere that is not this machine -----------------------
#
# The surface exists to be reached by a caller that is not Vigil, so the Host a
# real deployment carries -- a domain, a container name, a service name -- has
# to be one it answers. The MCP SDK defaults its host to 127.0.0.1 and, on that
# default, turns on DNS-rebinding protection with a localhost allow-list: every
# other Host is refused 421 before Vigil's own gates run. The tests above never
# reach that check, because 404 and 401 are answered first.
#
# The host is set as the client's base URL rather than as a header, because
# httpx treats a Host header that disagrees with the URL as cross-origin and
# drops the Authorization header -- which would fail this test for the wrong
# reason.


class _SomeoneWithACredential:
    username = "nestor"


@pytest.fixture
def client_on_a_domain():
    from fastapi.testclient import TestClient

    from services.api.main import app

    with TestClient(app, base_url="http://vigil.example.com") as c:
        yield c


def test_a_caller_on_a_domain_reaches_the_server(client_on_a_domain):
    """A deployment behind a domain name is the point, not an edge case."""
    with patch("services.api.mcp_surface.is_enabled", return_value=True), patch(
        "services.api.mcp_surface.authenticate",
        return_value=_SomeoneWithACredential(),
    ):
        response = client_on_a_domain.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "not-vigil", "version": "1"},
                },
            },
            headers={
                "Authorization": "Bearer vgl_mcp_a_working_one",
                "Accept": "application/json, text/event-stream",
            },
        )

    assert response.status_code != 421, (
        "The MCP surface refused a Host that is not localhost. Its transport "
        "security is being inferred from the SDK's 127.0.0.1 default, which "
        "allow-lists localhost only -- so every deployment behind a domain, a "
        "container name or an ingress is unreachable."
    )
    assert response.status_code == 200


# --- A session belongs to whoever opened it ----------------------------------
#
# The session manager compares the principal on each request against the one
# recorded when the session was created -- but only when scope["user"] is one
# of its own AuthenticatedUser. Vigil authenticates ahead of the server, so
# without _owned_by the principal is None on every request, None matches None,
# and the check passes for anyone.


class _Alice:
    username = "alice"
    user_id = "u-alice"


class _Bob:
    username = "bob"
    user_id = "u-bob"


_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "not-vigil", "version": "1"},
    },
}
_MCP_HEADERS = {
    "Authorization": "Bearer vgl_mcp_a_working_one",
    "Accept": "application/json, text/event-stream",
}


def test_one_callers_session_is_not_another_callers(client_on_a_domain):
    """Learning a session id must not be enough to act on that session."""
    with patch("services.api.mcp_surface.is_enabled", return_value=True):
        with patch("services.api.mcp_surface.authenticate", return_value=_Alice()):
            opened = client_on_a_domain.post(
                "/mcp", json=_INITIALIZE, headers=_MCP_HEADERS
            )
        assert opened.status_code == 200
        session_id = opened.headers.get("mcp-session-id")
        assert session_id, "the server did not hand back a session id"

        with patch("services.api.mcp_surface.authenticate", return_value=_Bob()):
            borrowed = client_on_a_domain.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                headers={**_MCP_HEADERS, "mcp-session-id": session_id},
            )

    assert borrowed.status_code == 404, (
        "A caller reached a session opened by someone else. The session "
        "manager only compares principals when scope['user'] is its own "
        "AuthenticatedUser; Vigil authenticates ahead of the server, so the "
        "principal must be put on the scope for the check to mean anything."
    )
