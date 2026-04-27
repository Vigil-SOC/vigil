"""Tests for the mempalace health endpoint (#136).

The endpoint must never raise — it's a status probe and the UI relies on
it returning shape even when mempalace is completely down. These tests
exercise the major degraded-path branches.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Mirror backend/main.py's sys.path setup so intra-package imports resolve
# without pulling in the entire backend/api/__init__.py chain.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND_DIR = _REPO_ROOT / "backend"
for p in (str(_REPO_ROOT), str(_BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load_config_module():
    spec = importlib.util.spec_from_file_location(
        "config_under_test", _BACKEND_DIR / "api" / "config.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["config_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def palace_dir(tmp_path, monkeypatch):
    """Point MEMPALACE_PALACE_PATH at a fresh tmp directory."""
    palace = tmp_path / "palace"
    palace.mkdir()
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(palace))
    return palace


@pytest.fixture()
def client(palace_dir):
    config_mod = _load_config_module()
    app = FastAPI()
    app.include_router(config_mod.router, prefix="/api/config")
    return TestClient(app)


@pytest.mark.unit
def test_health_returns_shape_when_no_mcp_client(client, palace_dir, monkeypatch):
    """No MCP client wired in → connected=false, no error, palace facts populated."""
    import services.mcp_client as mcp_client_mod

    monkeypatch.setattr(mcp_client_mod, "get_mcp_client", lambda: None)

    resp = client.get("/api/config/mempalace/health")
    assert resp.status_code == 200
    data = resp.json()

    assert data["connected"] is False
    assert data["palace_path"] == str(palace_dir)
    assert data["palace_exists"] is True
    assert data["size_bytes"] == 0
    assert data["closed_cases_count"] == 0
    # chromadb isn't installed in dev — the endpoint must degrade gracefully.
    assert data["memories_count_source"] in ("chromadb", "unavailable")


@pytest.mark.unit
def test_health_counts_closed_cases(client, palace_dir, monkeypatch):
    """JSON files dropped in investigations/closed-cases/ should be counted."""
    import services.mcp_client as mcp_client_mod

    monkeypatch.setattr(mcp_client_mod, "get_mcp_client", lambda: None)

    closed = palace_dir / "investigations" / "closed-cases"
    closed.mkdir(parents=True)
    for i in range(3):
        (closed / f"case-{i}.json").write_text(json.dumps({"i": i}))
    # A non-json file must be ignored.
    (closed / "README.txt").write_text("ignore me")

    resp = client.get("/api/config/mempalace/health")
    data = resp.json()
    assert data["closed_cases_count"] == 3
    assert data["size_bytes"] > 0
    assert data["last_modified_iso"] is not None


@pytest.mark.unit
def test_health_handles_missing_palace(client, tmp_path, monkeypatch):
    """If the palace path is unreachable, palace_exists must be False, not raise."""
    missing = tmp_path / "does-not-exist-xyz"
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(missing))
    import services.mcp_client as mcp_client_mod

    monkeypatch.setattr(mcp_client_mod, "get_mcp_client", lambda: None)

    resp = client.get("/api/config/mempalace/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["palace_exists"] is False
    assert data["size_bytes"] is None
    assert data["last_modified_iso"] is None
    assert data["closed_cases_count"] == 0


@pytest.mark.unit
def test_health_surfaces_mcp_error(client, palace_dir, monkeypatch):
    """When the MCP client reports mempalace as connected with no error,
    the endpoint should reflect that. When disconnected with a last_error
    the error string must propagate so operators see the real failure."""
    import services.mcp_client as mcp_client_mod

    class FakeClient:
        def get_connection_status(self):
            return {"mempalace": False, "splunk": True}

        def get_last_error(self, name):
            return "stdio process exited with code 1" if name == "mempalace" else None

    monkeypatch.setattr(mcp_client_mod, "get_mcp_client", lambda: FakeClient())

    resp = client.get("/api/config/mempalace/health")
    data = resp.json()
    assert data["connected"] is False
    assert data["error"] == "stdio process exited with code 1"


@pytest.mark.unit
def test_health_connected_path(client, palace_dir, monkeypatch):
    """Happy path — MCP says connected, no error string surfaced."""
    import services.mcp_client as mcp_client_mod

    class FakeClient:
        def get_connection_status(self):
            return {"mempalace": True}

        def get_last_error(self, name):
            return None

    monkeypatch.setattr(mcp_client_mod, "get_mcp_client", lambda: FakeClient())

    resp = client.get("/api/config/mempalace/health")
    data = resp.json()
    assert data["connected"] is True
    assert data["error"] is None
