"""security-detections waits for its rules instead of reading as disconnected (#2291).

A fresh install has no detection rules until they are fetched. The server is
not started until then, and every surface reports it as not set up -- with the
fetch as its one action -- rather than as a server that went down.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.deps import provide_mcp_client, provide_mcp_registry
from core.detections import detection_rules_service as drs
from core.detections.detection_rules_service import DetectionRulesService
from core.integrations.mcp.client import MCPClient
from core.integrations.mcp.service import MCPServer, MCPService
from services.api.routers import mcp as mcp_api

pytestmark = pytest.mark.unit

SD = "security-detections"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(drs, "_safe_home", lambda: tmp_path)

    def fake_vigil_path(*parts, write=False):
        target = tmp_path.joinpath(".vigil", *parts)
        if write:
            target.parent.mkdir(parents=True, exist_ok=True)
        return target

    monkeypatch.setattr(drs, "vigil_path", fake_vigil_path)
    return tmp_path


def _fetch_sigma(home):
    """What ``update-all`` leaves on disk for the Sigma source."""
    rules = home / "security-detections" / "sigma" / "rules"
    rules.mkdir(parents=True)
    (rules / "x.yml").write_text("title: x\n")
    DetectionRulesService().rescan_sources()  # saves the source as ready
    return rules


def _service(rules: DetectionRulesService) -> MCPService:
    service = MCPService.__new__(MCPService)
    service._detection_rules = rules
    service._enabled_servers = {}
    service.servers = {
        SD: MCPServer(name=SD, command="node", args=[], cwd=".", env={"KEEP": "1"})
    }
    return service


def test_a_fresh_install_needs_its_rules_fetched(home):
    service = _service(DetectionRulesService())

    assert service.setup_required(SD)
    assert service.setup_action(SD) == "fetch_detection_rules"
    assert service.setup_required("splunk-selfhosted") is None


def test_rules_fetched_by_another_worker_clear_the_gate(home):
    # This worker loaded before the fetch; another one ran it.
    service = _service(DetectionRulesService())
    rules = _fetch_sigma(home)

    assert service.setup_required(SD) is None
    env = service.servers[SD].env
    assert env["SIGMA_PATHS"] == str(rules)
    assert env["KEEP"] == "1"


@pytest.mark.asyncio
async def test_the_server_is_not_spawned_until_set_up(home):
    service = _service(DetectionRulesService())
    service.is_server_enabled = lambda name: True
    client = MCPClient(service)

    with patch(
        "core.integrations.mcp.client.PersistentServerSession.connect",
        new_callable=AsyncMock,
        return_value=True,
    ) as spawn:
        assert await client.connect_to_server(SD) is False
        spawn.assert_not_called()
        assert client.held_for_setup(SD)
        assert client.get_last_error(SD) is None
        assert not client.was_dialled(SD)

        result = await client.call_tool(SD, "search", {})
        assert "not set up" in result["content"][0]["text"]

        _fetch_sigma(home)
        with patch.object(client, "_RETRY_MIN_INTERVAL_S", 0):
            await client.retry_dormant_if_ready()
        spawn.assert_awaited()


@pytest.fixture
def api():
    app = FastAPI()
    app.include_router(mcp_api.router, prefix="/api/mcp")
    return TestClient(app)


def _stub(connected=None, dialled=(), held=()):
    return SimpleNamespace(
        get_connection_status=lambda: dict(connected or {}),
        get_last_error=lambda name: None,
        get_missing_credentials=lambda name: None,
        was_dialled=lambda name: name in dialled,
        held_for_setup=lambda name: name in held,
        retry_dormant_if_ready=AsyncMock(),
    )


def _statuses(api, stub, *, needs_setup, eager):
    api.app.dependency_overrides[provide_mcp_client] = lambda: stub
    reason = "No detection rules have been fetched on this install yet."
    with patch.object(
        mcp_api.mcp_service, "list_servers", return_value=[SD, "splunk-selfhosted"]
    ), patch.object(
        mcp_api.mcp_service,
        "get_all_enabled_states",
        return_value={SD: True, "splunk-selfhosted": True},
    ), patch.object(
        mcp_api.mcp_service,
        "setup_required",
        side_effect=lambda name: reason if needs_setup and name == SD else None,
    ), patch.object(
        mcp_api.mcp_service,
        "setup_action",
        side_effect=lambda name: "fetch_detection_rules" if name == SD else None,
    ), patch(
        "core.integrations.mcp.registry.eager_connect_enabled", return_value=eager
    ):
        response = api.get("/api/mcp/servers/status")
    assert response.status_code == 200
    return {row["name"]: row for row in response.json()["statuses"]}


def test_status_says_not_set_up_with_the_fetch(api):
    rows = _statuses(
        api, _stub(dialled={"splunk-selfhosted"}), needs_setup=True, eager=True
    )

    assert rows[SD] == {
        "name": SD,
        "status": "not_set_up",
        "enabled": True,
        "setup": "No detection rules have been fetched on this install yet.",
        "setup_action": "fetch_detection_rules",
    }


def test_status_says_on_demand_for_a_server_nothing_has_used(api):
    # DEV_MODE's default: no eager connect, so an untouched server is waiting.
    rows = _statuses(api, _stub(), needs_setup=False, eager=False)

    assert rows["splunk-selfhosted"]["status"] == "on_demand"
    assert rows[SD]["status"] == "on_demand"


def test_status_says_on_demand_once_another_worker_fetched_the_rules(api):
    rows = _statuses(
        api,
        _stub(dialled={"splunk-selfhosted"}, held={SD}),
        needs_setup=False,
        eager=True,
    )

    assert rows[SD]["status"] == "on_demand"
    assert rows["splunk-selfhosted"]["status"] == "disconnected"


def test_enabling_before_set_up_keeps_the_switch_on(api):
    stub = _stub()
    stub.connect_to_server = AsyncMock(return_value=False)
    api.app.dependency_overrides[provide_mcp_client] = lambda: stub
    api.app.dependency_overrides[mcp_api.get_current_active_user] = (
        lambda: SimpleNamespace(user_id="admin")
    )
    api.app.dependency_overrides[provide_mcp_registry] = lambda: None
    with patch.object(mcp_api, "require_integrations_admin"), patch.object(
        mcp_api.mcp_service, "list_servers", return_value=[SD]
    ), patch.object(
        mcp_api.mcp_service, "set_server_enabled", return_value=True
    ), patch.object(
        mcp_api.mcp_service, "setup_required", return_value="no rules"
    ):
        response = api.put(f"/api/mcp/servers/{SD}/enabled", json={"enabled": True})

    body = response.json()
    assert (body["enabled"], body["connected"], body["setup"]) == (
        True,
        None,
        "no rules",
    )
    stub.connect_to_server.assert_not_called()
