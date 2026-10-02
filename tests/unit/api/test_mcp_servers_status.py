"""GET /api/mcp/servers/status reports MCPClient session state, not the catalog."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.deps import provide_mcp_client
from services.api.routers import mcp as mcp_api

pytestmark = pytest.mark.unit


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(mcp_api.router, prefix="/api/mcp")
    return TestClient(app)


def _catalog():
    return patch.object(
        mcp_api.mcp_service,
        "list_servers",
        return_value=["github", "virustotal", "slack"],
    ), patch.object(
        mcp_api.mcp_service,
        "get_all_enabled_states",
        return_value={"github": True, "virustotal": True, "slack": False},
    )


def test_status_route_reads_the_stubbed_client(client):
    stub = SimpleNamespace(
        get_connection_status=lambda: {
            "github": True,
            "virustotal": False,
            "slack": False,
        },
        get_last_error=lambda name: {"virustotal": "connection refused"}.get(name),
        get_missing_credentials=lambda name: {"virustotal": ["VT_API_KEY"]}.get(name),
        retry_dormant_if_ready=AsyncMock(),
    )
    client.app.dependency_overrides[provide_mcp_client] = lambda: stub
    servers, enabled = _catalog()
    with servers, enabled:
        response = client.get("/api/mcp/servers/status")

    assert response.status_code == 200
    rows = {row["name"]: row for row in response.json()["statuses"]}
    assert rows["github"] == {"name": "github", "status": "running", "enabled": True}
    assert rows["virustotal"] == {
        "name": "virustotal",
        "status": "disconnected",
        "enabled": True,
        "missing_credentials": ["VT_API_KEY"],
        "error": "connection refused",
    }
    assert rows["slack"] == {
        "name": "slack",
        "status": "disconnected",
        "enabled": False,
    }
    stub.retry_dormant_if_ready.assert_not_called()


def test_status_route_reports_disconnected_when_the_client_is_missing(client):
    client.app.dependency_overrides[provide_mcp_client] = lambda: None
    servers, enabled = _catalog()
    with servers, enabled:
        response = client.get("/api/mcp/servers/status")

    assert response.status_code == 200
    rows = response.json()["statuses"]
    assert [row["status"] for row in rows] == ["disconnected"] * 3
    assert [row["enabled"] for row in rows] == [True, True, False]
    assert all("error" not in row and "missing_credentials" not in row for row in rows)
