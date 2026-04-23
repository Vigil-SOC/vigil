"""API-contract integration tests for the transactional MCP enable toggle (#125).

These tests verify that ``PUT /api/mcp/servers/{name}/enabled`` does more
than persist a bit — it also triggers a connect/disconnect at runtime and
returns the result in the response envelope. Without the transactional
behavior, enabling a server was a no-op until the backend was restarted,
which is the bug #125 exists to fix.

The tests stub ``mcp_service`` and ``mcp_client`` so they don't spawn real
MCP child processes or require credentials. See tests/integration/conftest.py
for why DB-backed fixtures aren't available here; this is a contract check,
not an end-to-end MCP spin-up.
"""

from __future__ import annotations

import os

# Keep CSRF out of the way — exercised elsewhere.
os.environ.setdefault("DEV_MODE", "true")
os.environ.setdefault("VIGIL_CSRF_ENABLED", "false")

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from backend.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_server_known():
    """Patch mcp_service so ``deeptempo-findings`` is a known, settable server."""
    from backend.api import mcp as mcp_api

    # Make set_server_enabled succeed (server exists); status is the stdio
    # sentinel so the legacy stop_server branch doesn't fire.
    with patch.object(
        mcp_api.mcp_service, "set_server_enabled", return_value=True
    ), patch.object(
        mcp_api.mcp_service,
        "get_server_status",
        return_value="stdio (MCP integration)",
    ):
        yield


@pytest.mark.integration
class TestEnableTransactional:
    """PUT /enabled should connect-on-enable and disconnect-on-disable."""

    def test_enable_triggers_connect_and_reports_connected(
        self, client, fake_server_known
    ):
        fake_client = MagicMock()
        fake_client.connect_to_server = AsyncMock(return_value=True)
        fake_client.get_last_error = MagicMock(return_value=None)
        fake_client.disconnect_from_server = AsyncMock(return_value=True)

        with patch(
            "services.mcp_client.get_mcp_client", return_value=fake_client
        ):
            r = client.put(
                "/api/mcp/servers/deeptempo-findings/enabled",
                json={"enabled": True},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enabled"] is True
        assert body["connected"] is True
        assert body["error"] is None
        fake_client.connect_to_server.assert_awaited_once_with(
            "deeptempo-findings", persistent=True
        )
        fake_client.disconnect_from_server.assert_not_called()

    def test_enable_with_missing_creds_reports_error_not_raising(
        self, client, fake_server_known
    ):
        # Simulate the credential-missing / FileNotFoundError / etc. case.
        fake_client = MagicMock()
        fake_client.connect_to_server = AsyncMock(return_value=False)
        fake_client.get_last_error = MagicMock(
            return_value="missing credentials: VIRUSTOTAL_API_KEY"
        )
        fake_client.disconnect_from_server = AsyncMock(return_value=True)

        with patch(
            "services.mcp_client.get_mcp_client", return_value=fake_client
        ):
            r = client.put(
                "/api/mcp/servers/virustotal/enabled",
                json={"enabled": True},
            )

        # Endpoint doesn't raise — it surfaces the reason so the UI can
        # flip the toggle back off and show a snackbar.
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enabled"] is True
        assert body["connected"] is False
        assert "VIRUSTOTAL_API_KEY" in (body["error"] or "")

    def test_disable_triggers_disconnect(self, client, fake_server_known):
        fake_client = MagicMock()
        fake_client.connect_to_server = AsyncMock(return_value=True)
        fake_client.disconnect_from_server = AsyncMock(return_value=True)

        with patch(
            "services.mcp_client.get_mcp_client", return_value=fake_client
        ):
            r = client.put(
                "/api/mcp/servers/deeptempo-findings/enabled",
                json={"enabled": False},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enabled"] is False
        # connected is None when disabling — we didn't attempt a connect.
        assert body["connected"] is None
        fake_client.disconnect_from_server.assert_awaited_once_with(
            "deeptempo-findings"
        )
        fake_client.connect_to_server.assert_not_called()


@pytest.mark.integration
class TestDeadEndpointsGone:
    """The broken /start + /stop paths should no longer exist."""

    def test_start_endpoint_is_removed(self, client):
        r = client.post("/api/mcp/servers/deeptempo-findings/start")
        # Either 404 (route not registered) or 405 (method not allowed) is
        # acceptable — just never a 500 or 200 from the old broken handler.
        assert r.status_code in (404, 405), r.text

    def test_start_all_endpoint_is_removed(self, client):
        r = client.post("/api/mcp/servers/start-all")
        assert r.status_code in (404, 405), r.text
