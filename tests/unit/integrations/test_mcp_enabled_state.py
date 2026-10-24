import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.deps import provide_mcp_client, provide_mcp_registry
from core.integrations.mcp.service import MCPService
from services.api.errors import register_exception_handlers
from services.api.middleware.auth import get_current_active_user
from services.api.routers import mcp as mcp_api

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file modes"
    ),
]


@pytest.fixture
def world(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("VIGIL_DIR", str(state_dir))
    entry = {"command": "node", "args": [], "env": {}}
    (tmp_path / "mcp-config.json").write_text(
        json.dumps({"mcpServers": {"geo": entry, "vt": entry}})
    )
    svc = MCPService(
        project_root=tmp_path,
        integration_bridge=MagicMock(),
        detection_rules=MagicMock(),
    )
    yield SimpleNamespace(
        svc=svc, dir=state_dir, file=state_dir / MCPService._STATE_FILENAME
    )
    state_dir.chmod(0o700)
    for f in state_dir.iterdir():
        f.chmod(0o600)


def test_no_file_reads_as_defaults(world):
    assert world.svc.get_all_enabled_states() == {"geo": False, "vt": False}


def test_unreadable_file_fails_the_toggle_and_keeps_the_others(world):
    world.svc.set_server_enabled("geo", True)
    world.file.chmod(0)

    with pytest.raises(PermissionError):
        world.svc.set_server_enabled("vt", True)

    world.file.chmod(0o600)
    assert json.loads(world.file.read_text()) == {"enabled": {"geo": True}}


def test_corrupt_file_fails_the_toggle_and_is_left_alone(world):
    world.file.write_text("{not json")

    with pytest.raises(json.JSONDecodeError):
        world.svc.set_server_enabled("vt", True)

    assert world.file.read_text() == "{not json"


def test_unwritable_dir_fails_the_toggle(world):
    world.dir.chmod(0o500)

    with pytest.raises(PermissionError):
        world.svc.set_server_enabled("geo", True)

    assert world.svc.is_server_enabled("geo") is False


def test_put_answers_500_when_the_state_cannot_be_saved(world):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(mcp_api.router, prefix="/api/mcp")
    app.dependency_overrides[get_current_active_user] = lambda: SimpleNamespace(
        user_id="admin"
    )
    client = MagicMock()
    app.dependency_overrides[provide_mcp_client] = lambda: client
    app.dependency_overrides[provide_mcp_registry] = lambda: MagicMock()
    world.dir.chmod(0o500)

    with patch.object(mcp_api, "_service", return_value=world.svc), patch.object(
        mcp_api, "require_integrations_admin"
    ):
        resp = TestClient(app, raise_server_exceptions=False).put(
            "/api/mcp/servers/geo/enabled", json={"enabled": True}
        )

    assert resp.status_code == 500
    client.connect_to_server.assert_not_called()
