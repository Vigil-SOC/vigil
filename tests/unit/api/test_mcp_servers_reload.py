"""POST /api/mcp/servers/reload applies mcp-config.json to running servers (#2183)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.deps import provide_mcp_client, provide_mcp_registry
from core.integrations.mcp.client import MCPClient
from core.integrations.mcp.service import MCPService
from services.api.middleware.auth import get_current_active_user
from services.api.routers import mcp as mcp_api

pytestmark = pytest.mark.unit


class _Session:
    is_connected = True

    def __init__(self):
        self.disconnect = AsyncMock()


def _write(root, servers):
    (root / "mcp-config.json").write_text(json.dumps({"mcpServers": servers}))


def _entry(*args, env=None):
    return {"command": "node", "args": list(args), "env": env or {}}


@pytest.fixture
def world(tmp_path):
    _write(tmp_path, {"geo": _entry("geo.js"), "kept": _entry("kept.js")})
    svc = MCPService(
        project_root=tmp_path,
        integration_bridge=MagicMock(),
        detection_rules=MagicMock(),
    )
    svc._load_enabled_state = lambda: {"geo": True, "kept": True}
    client = MCPClient(svc)
    for name in svc.servers:
        client.persistent_sessions[name] = _Session()
        client.tools_cache[name] = [{"name": f"{name}_tool"}]

    async def connect(name, persistent=True):
        client.persistent_sessions[name] = _Session()
        client.tools_cache[name] = [{"name": f"{name}_tool"}]
        return True

    client.connect_to_server = AsyncMock(side_effect=connect)
    registry = MagicMock()

    app = FastAPI()
    app.include_router(mcp_api.router, prefix="/api/mcp")
    app.dependency_overrides[get_current_active_user] = lambda: SimpleNamespace(
        user_id="admin"
    )
    app.dependency_overrides[provide_mcp_client] = lambda: client
    app.dependency_overrides[provide_mcp_registry] = lambda: registry
    with patch.object(mcp_api, "_service", return_value=svc), patch.object(
        mcp_api, "require_integrations_admin"
    ):
        yield SimpleNamespace(
            root=tmp_path,
            svc=svc,
            client=client,
            registry=registry,
            http=TestClient(app),
        )


def _reload(w):
    return w.http.post("/api/mcp/servers/reload")


def test_bad_json_keeps_the_catalog_and_sessions(world):
    sessions = dict(world.client.persistent_sessions)
    (world.root / "mcp-config.json").write_text("{not json")

    response = _reload(world)

    assert response.status_code == 422
    assert "JSONDecodeError" in response.json()["detail"]
    assert sorted(world.svc.servers) == ["geo", "kept"]
    assert world.client.persistent_sessions == sessions


def test_changed_env_reconnects_and_unchanged_is_left_alone(world):
    geo, kept = (world.client.persistent_sessions[n] for n in ("geo", "kept"))
    _write(
        world.root,
        {"geo": _entry("geo.js", env={"X": "1"}), "kept": _entry("kept.js")},
    )

    body = _reload(world).json()

    assert body["success"] is True
    assert body["reconnected"] == ["geo"] and body["disconnected"] == []
    geo.disconnect.assert_awaited_once()
    assert world.client.persistent_sessions["geo"] is not geo
    assert world.svc.servers["geo"].env["X"] == "1"
    assert world.client.persistent_sessions["kept"] is kept
    kept.disconnect.assert_not_awaited()
    world.client.connect_to_server.assert_awaited_once_with("geo", persistent=True)
    world.registry.register_server.assert_called_once()


def test_changed_args_reconnects(world):
    _write(world.root, {"geo": _entry("geo.js", "--v2"), "kept": _entry("kept.js")})

    assert _reload(world).json()["reconnected"] == ["geo"]


def test_failed_reconnect_reports_and_deactivates(world):
    world.client.connect_to_server = AsyncMock(return_value=False)
    world.client.last_errors["geo"] = "boom"
    _write(world.root, {"geo": _entry("geo.js", "--v2"), "kept": _entry("kept.js")})

    body = _reload(world).json()

    assert body["success"] is False
    assert body["failed"] == {"geo": "boom"}
    world.registry.set_active.assert_called_once_with("geo", False)


def test_removed_server_is_disconnected_and_deactivated(world):
    geo = world.client.persistent_sessions["geo"]
    _write(world.root, {"kept": _entry("kept.js")})

    body = _reload(world).json()

    assert body["disconnected"] == ["geo"] and body["servers"] == ["kept"]
    geo.disconnect.assert_awaited_once()
    assert "geo" not in world.client.persistent_sessions
    assert "geo" not in world.client.tools_cache
    world.registry.set_active.assert_called_once_with("geo", False)


def test_runner_mode_stops_removed_and_ignores_args(world):
    url = lambda name: "http://runner"  # noqa: E731
    stop = AsyncMock()
    kept = world.client.persistent_sessions["kept"]
    _write(world.root, {"kept": _entry("kept.js", "--only-args-changed")})

    with patch("core.integrations.mcp.runner.runner_url", url), patch(
        "core.integrations.mcp.client.runner_url", url
    ), patch("core.integrations.mcp.client.RunnerServer.stop", stop):
        body = _reload(world).json()

    assert body["disconnected"] == ["geo"] and body["reconnected"] == []
    stop.assert_awaited_once()  # geo's DELETE; kept is not bounced
    assert world.client.persistent_sessions["kept"] is kept
