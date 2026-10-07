"""POST /api/config/integrations/{id}/test actually probes MCP servers."""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.deps import provide_integration_bridge, provide_mcp_client
from core.integrations.integration_bridge_service import IntegrationBridgeService
from core.integrations.mcp.client import MCPClient
from core.integrations.mcp.service import MCPServer
from services.api.routers.config import router as config_router

pytestmark = pytest.mark.unit


class _Session:
    def __init__(self, error: Optional[Exception] = None):
        self.error = error
        self.listed = 0

    async def list_tools(self):
        self.listed += 1
        if self.error is not None:
            raise self.error
        return []


class _Holder:
    def __init__(self, session: _Session):
        self.session = session
        self.is_connected = True


class _Service:
    def __init__(self, enabled: Tuple[str, ...] = ()):
        self.enabled = set(enabled)
        self.reloads = 0

    def is_server_enabled(self, name: str) -> bool:
        return name in self.enabled

    def reload_server_configs(self) -> None:
        self.reloads += 1


class _Client:
    def __init__(
        self,
        results: Dict[str, Tuple[bool, Optional[str], Optional[List[str]]]],
        enabled: Tuple[str, ...] = (),
    ):
        self.results = results
        self.mcp_service = _Service(enabled)
        self.persistent_sessions: Dict[str, _Holder] = {}
        self.calls: List[tuple] = []
        self._errors: Dict[str, str] = {}
        self._missing: Dict[str, List[str]] = {}

    async def connect_to_server(
        self,
        name: str,
        persistent: bool = True,
        skip_enabled_check: bool = False,
    ) -> bool:
        assert self.mcp_service.reloads == 1
        self.calls.append((name, persistent, skip_enabled_check))
        ok, error, missing = self.results[name]
        if error:
            self._errors[name] = error
        if missing:
            self._missing[name] = missing
        return ok

    def get_last_error(self, name: str) -> Optional[str]:
        return self._errors.get(name)

    def get_missing_credentials(self, name: str) -> Optional[List[str]]:
        return self._missing.get(name)


class _Config:
    def __init__(self):
        self.tests: List[dict] = []
        self.rewrites = 0

    def record_integration_test(self, integration_id, *, success, error, tested_at):
        self.tests.append(
            {
                "integration_id": integration_id,
                "success": success,
                "error": error,
                "tested_at": tested_at,
            }
        )
        return True

    def set_integration_config(self, *args, **kwargs):
        self.rewrites += 1
        return True


@pytest.fixture()
def client(authenticate_app):
    app = FastAPI()
    app.include_router(config_router, prefix="/api/config")
    authenticate_app(app)
    return TestClient(app)


@pytest.fixture()
def saved():
    return _Config()


def _bridge(
    integrations: dict, enabled: Optional[list] = None
) -> IntegrationBridgeService:
    bridge = IntegrationBridgeService()
    payload = {
        "enabled_integrations": enabled or [],
        "integrations": integrations,
    }
    bridge.load_integration_config = lambda: payload
    return bridge


def _post(client, integration_id, bridge, mcp, saved):
    client.app.dependency_overrides[provide_integration_bridge] = lambda: bridge
    client.app.dependency_overrides[provide_mcp_client] = lambda: mcp
    with patch("services.api.routers.config.get_config_service", return_value=saved):
        return client.post(f"/api/config/integrations/{integration_id}/test")


def test_failed_connect_stamps_failure(client, saved):
    mcp = _Client(
        {"virustotal": (False, "connection refused", None)},
        enabled=("virustotal",),
    )
    response = _post(
        client,
        "virustotal",
        _bridge({"virustotal": {}}),
        mcp,
        saved,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is False
    assert body["servers"] == [
        {"name": "virustotal", "success": False, "error": "connection refused"}
    ]
    assert saved.rewrites == 0
    assert saved.tests[0]["success"] is False
    assert saved.tests[0]["error"] == "virustotal: connection refused"
    assert saved.tests[0]["tested_at"] is not None
    assert mcp.calls == [("virustotal", True, False)]


def test_successful_connect_stamps_last_test(client, saved):
    mcp = _Client({"virustotal": (True, None, None)}, enabled=("virustotal",))
    response = _post(
        client, "virustotal", _bridge({"virustotal": {"region": "us"}}), mcp, saved
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert body["servers"][0]["success"] is True
    assert "error" not in body["servers"][0]
    assert saved.tests[0]["success"] is True
    assert saved.tests[0]["error"] is None
    assert saved.tests[0]["tested_at"] is not None


def test_secret_only_config_is_not_a_400(client, saved):
    """VirusTotal persists as {} once the API key is split into the secret store."""
    mcp = _Client(
        {
            "virustotal": (
                False,
                "missing credentials: VIRUSTOTAL_API_KEY",
                ["VIRUSTOTAL_API_KEY"],
            )
        },
        enabled=(),
    )
    response = _post(client, "virustotal", _bridge({"virustotal": {}}), mcp, saved)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is False
    assert body["servers"][0]["missing_credentials"] == ["VIRUSTOTAL_API_KEY"]
    assert mcp.calls == [("virustotal", False, True)]
    assert saved.tests[0]["success"] is False


def test_disabled_integration_flag_does_not_block_probe(client, saved):
    mcp = _Client({"virustotal": (True, None, None)}, enabled=("virustotal",))
    response = _post(
        client,
        "virustotal",
        _bridge({"virustotal": {}}, enabled=[]),
        mcp,
        saved,
    )

    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
    assert mcp.calls == [("virustotal", True, False)]


def test_splunk_reports_each_server_and_fails_closed(client, saved):
    mcp = _Client(
        {
            "splunk": (True, None, None),
            "splunk-selfhosted": (False, "connection refused", None),
        },
        enabled=("splunk", "splunk-selfhosted"),
    )
    response = _post(
        client,
        "splunk",
        _bridge({"splunk": {"server_url": "https://splunk.example"}}),
        mcp,
        saved,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is False
    assert [server["name"] for server in body["servers"]] == [
        "splunk",
        "splunk-selfhosted",
    ]
    assert body["servers"][0]["success"] is True
    assert body["servers"][1]["success"] is False
    assert saved.tests[0]["success"] is False
    assert "splunk-selfhosted" in saved.tests[0]["error"]


def test_no_enabled_server_probes_every_declared_server_temporarily(client, saved):
    mcp = _Client(
        {
            "splunk": (False, "down", None),
            "splunk-selfhosted": (False, "down", None),
        },
        enabled=(),
    )
    response = _post(
        client, "splunk", _bridge({"splunk": {"server_url": "x"}}), mcp, saved
    )

    assert response.status_code == 200, response.text
    assert response.json()["success"] is False
    assert mcp.calls == [
        ("splunk", False, True),
        ("splunk-selfhosted", False, True),
    ]


def test_catalog_entry_is_not_testable(client, saved):
    mcp = _Client({}, enabled=("github",))
    response = _post(client, "github", _bridge({}), mcp, saved)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is False
    assert body["reason"] == "not_testable"
    assert "not testable" in body["message"]
    assert saved.tests == []
    assert mcp.calls == []


def test_unconfigured_descriptor_is_400(client, saved):
    mcp = _Client({"virustotal": (True, None, None)}, enabled=("virustotal",))
    response = _post(client, "virustotal", _bridge({}), mcp, saved)

    assert response.status_code == 400
    assert saved.tests == []
    assert mcp.calls == []


def test_already_connected_session_lists_tools(client, saved):
    mcp = _Client({"virustotal": (True, None, None)}, enabled=("virustotal",))
    session = _Session()
    mcp.persistent_sessions["virustotal"] = _Holder(session)

    response = _post(client, "virustotal", _bridge({"virustotal": {}}), mcp, saved)

    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
    assert session.listed == 1
    assert saved.tests[0]["success"] is True


def test_already_connected_list_tools_failure_is_a_failed_probe(client, saved):
    mcp = _Client({"virustotal": (True, None, None)}, enabled=("virustotal",))
    session = _Session(error=RuntimeError("session closed"))
    mcp.persistent_sessions["virustotal"] = _Holder(session)

    response = _post(client, "virustotal", _bridge({"virustotal": {}}), mcp, saved)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is False
    assert "session closed" in body["servers"][0]["error"]
    assert saved.tests[0]["success"] is False


def test_requires_integrations_write(client, saved):
    mcp = _Client({"virustotal": (True, None, None)}, enabled=("virustotal",))
    client.app.dependency_overrides[provide_integration_bridge] = lambda: _bridge(
        {"virustotal": {}}
    )
    client.app.dependency_overrides[provide_mcp_client] = lambda: mcp
    with patch(
        "core.auth.auth_service.AuthService.check_permission", return_value=False
    ), patch("services.api.routers.config.get_config_service", return_value=saved):
        response = client.post("/api/config/integrations/virustotal/test")

    assert response.status_code == 403
    assert saved.tests == []
    assert mcp.calls == []


def test_missing_client_does_not_stamp(client, saved):
    response = _post(client, "virustotal", _bridge({"virustotal": {}}), None, saved)

    assert response.status_code == 200, response.text
    assert response.json()["success"] is False
    assert saved.tests == []


@pytest.mark.asyncio
async def test_skip_enabled_check_sets_last_error():
    server = MCPServer(
        name="demo",
        command="python",
        args=["-m", "demo"],
        cwd=".",
        env={},
        required_env_vars=["DEMO_TOKEN"],
    )

    class _Disabled:
        servers = {"demo": server}

        def is_server_enabled(self, name: str) -> bool:
            return False

    client = MCPClient(_Disabled())
    with patch("core.integrations.mcp.client.MCP_AVAILABLE", True), patch(
        "core.integrations.mcp.client.get_secret", return_value=None
    ):
        skipped = await client.connect_to_server("demo", persistent=False)
        assert skipped is False
        assert client.get_last_error("demo") is None

        probed = await client.connect_to_server(
            "demo", persistent=False, skip_enabled_check=True
        )

    assert probed is False
    assert client.get_missing_credentials("demo") == ["DEMO_TOKEN"]
    assert client.get_last_error("demo")


@pytest.mark.asyncio
async def test_persistent_connect_failure_is_recorded():
    """Enabled servers use persistent=True. A failed spawn must still set last_error."""
    server = MCPServer(
        name="demo",
        command="python",
        args=["-m", "demo"],
        cwd=".",
        env={},
    )

    class _Enabled:
        servers = {"demo": server}

        def is_server_enabled(self, name: str) -> bool:
            return True

    client = MCPClient(_Enabled())

    def _boom(*_args, **_kwargs):
        raise FileNotFoundError("uvx")

    with patch("core.integrations.mcp.client.MCP_AVAILABLE", True), patch(
        "core.integrations.mcp.client.StdioServerParameters",
        side_effect=lambda **_kw: object(),
    ), patch("core.integrations.mcp.client.stdio_client", side_effect=_boom):
        ok = await client.connect_to_server("demo", persistent=True)

    assert ok is False
    assert client.get_last_error("demo") == "FileNotFoundError: uvx"
