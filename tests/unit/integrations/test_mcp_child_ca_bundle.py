"""The CA bundle reaching a spawned MCP tool server.

The servers run on httpx, which reads SSL_CERT_FILE/SSL_CERT_DIR and ignores
REQUESTS_CA_BUNDLE/CURL_CA_BUNDLE. Two things have to hold: the legacy names
translate, and the result is passed explicitly, because stdio_client narrows the
child environment to a six-name allowlist.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.client.stdio import get_default_environment

from core.integrations.mcp.child_env import ca_bundle_env
from core.integrations.mcp.client import MCPClient
from core.integrations.mcp.service import MCPServer, MCPService

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]

# Child env is assembled in _initialize_servers (parent snapshot) and spawned
# once via StdioServerParameters. os.environ.copy() there is the snapshot, not
# a second spawn — the deleted Popen path used to make that ambiguous.
CHILD_ENV_SITES = (
    ("core/integrations/mcp/service.py", "os.environ.copy()"),
    ("core/integrations/mcp/client.py", "StdioServerParameters("),
)

_ALL_VARS = ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


@pytest.fixture(autouse=True)
def _clear_bundle_vars(monkeypatch):
    for var in _ALL_VARS:
        monkeypatch.delenv(var, raising=False)


def test_no_bundle_configured_leaves_httpx_on_certifi():
    assert ca_bundle_env() == {}


@pytest.mark.parametrize("var", _ALL_VARS[:1] + _ALL_VARS[2:])
def test_a_file_bundle_arrives_as_ssl_cert_file(monkeypatch, tmp_path, var):
    """httpx reads SSL_CERT_FILE; npx/Node reads NODE_EXTRA_CA_CERTS."""
    bundle = tmp_path / "corporate-ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setenv(var, str(bundle))

    assert ca_bundle_env() == {
        "SSL_CERT_FILE": str(bundle),
        "NODE_EXTRA_CA_CERTS": str(bundle),
    }


def test_a_directory_bundle_arrives_as_ssl_cert_dir(monkeypatch, tmp_path):
    """cafile and capath are not interchangeable — route by what the path is."""
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path))

    assert ca_bundle_env() == {"SSL_CERT_DIR": str(tmp_path)}


def test_the_httpx_native_name_wins(monkeypatch, tmp_path):
    """An operator who already moved on is not overridden by a stale value."""
    current = tmp_path / "current.pem"
    current.write_text("x")
    legacy = tmp_path / "legacy.pem"
    legacy.write_text("x")
    monkeypatch.setenv("SSL_CERT_FILE", str(current))
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(legacy))

    assert ca_bundle_env() == {
        "SSL_CERT_FILE": str(current),
        "NODE_EXTRA_CA_CERTS": str(current),
    }


def test_a_missing_path_falls_back_rather_than_breaking_tls(monkeypatch, tmp_path):
    """A stale path must not become an SSL_CERT_FILE that fails every call."""
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "gone.pem"))

    assert ca_bundle_env() == {}


def test_the_sdk_would_otherwise_strip_it(monkeypatch, tmp_path):
    """Why this is forwarded and not inherited.

    If stdio_client passed the parent environment through, none of the above
    would be needed. It doesn't — so guard the assumption.
    """
    bundle = tmp_path / "ca.pem"
    bundle.write_text("x")
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))

    assert "SSL_CERT_FILE" not in get_default_environment()
    assert "NODE_EXTRA_CA_CERTS" not in get_default_environment()


@pytest.mark.parametrize("rel, env_built", CHILD_ENV_SITES)
def test_every_child_env_site_forwards_the_bundle(rel, env_built):
    """One ca_bundle_env() per place a child environment is built."""
    src = (ROOT / rel).read_text()
    built = src.count(env_built)
    forwards = src.count("ca_bundle_env()")
    assert built >= 1
    assert forwards >= built, (
        f"{rel} builds {built} child environment(s) but forwards the CA "
        f"bundle {forwards} time(s) — a server spawned without it fails TLS "
        "behind a private CA"
    )


def test_mcp_server_does_not_popen_its_own_child():
    """Runtime spawn is StdioServerParameters only; the Popen start is gone."""
    assert not hasattr(MCPServer, "start")
    service = (ROOT / "core/integrations/mcp/service.py").read_text()
    assert "subprocess.Popen(" not in service


def test_initialize_servers_snapshots_parent_env_into_server_env(tmp_path, monkeypatch):
    """The live child env is the parent snapshot plus declared config entries."""
    monkeypatch.setenv("VIGIL_PARENT_MARKER", "inherited")
    (tmp_path / "mcp-config.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "demo": {
                        "command": "python",
                        "args": ["-m", "tools.demo"],
                        "env": {"CUSTOM_FROM_CONFIG": "yes"},
                    }
                }
            }
        )
    )
    service = MCPService(project_root=tmp_path)
    env = service.servers["demo"].env
    assert env["VIGIL_PARENT_MARKER"] == "inherited"
    assert env["CUSTOM_FROM_CONFIG"] == "yes"
    assert env["PYTHONPATH"] == str(tmp_path)


@pytest.mark.asyncio
async def test_stdio_params_receive_the_parent_snapshot_from_server_env():
    """connect_to_server forwards server.env into the one spawn site as-is."""
    snapshot = {"PARENT_MARKER": "from-parent", "PATH": "/bin"}
    server = MCPServer(
        name="demo",
        command="python",
        args=["-m", "tools.demo"],
        cwd=".",
        env=snapshot,
    )

    class _StubService:
        servers = {"demo": server}

        def is_server_enabled(self, name: str) -> bool:
            return True

    client = MCPClient(_StubService())
    captured: dict = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    with patch(
        "core.integrations.mcp.client.StdioServerParameters",
        side_effect=lambda **kw: _capture(**kw),
    ), patch(
        "core.integrations.mcp.client.PersistentServerSession.connect",
        new_callable=AsyncMock,
        return_value=False,
    ):
        await client.connect_to_server("demo", persistent=True)

    assert captured["env"]["PARENT_MARKER"] == "from-parent"
    assert captured["command"] == "python"
    assert captured["args"] == ["-m", "tools.demo"]
