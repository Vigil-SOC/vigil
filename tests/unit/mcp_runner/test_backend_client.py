"""The backend's MCP client against the real runner app, in process.

With a runner URL set the client PUTs the server with the env it resolved and
speaks streamable HTTP; nothing goes through stdio, the reconnect included.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import anyio
import httpx2
import pytest

import core.integrations.mcp.client as client_mod
import core.integrations.mcp.runner as runner_mod
from core.integrations._base.descriptor import IntegrationDescriptor, IntegrationField
from core.integrations.mcp.client import MCPClient
from core.integrations.mcp.service import MCPServer
from services.mcp_runner.main import create_app
from tests.unit.mcp_runner.test_runner import (  # noqa: F401 - fixture
    TOKEN,
    StreamingASGITransport,
    catalog,
)

pytestmark = pytest.mark.unit


class _Service:
    def __init__(self, server):
        self.servers = {server.name: server}
        self.enabled = True

    def is_server_enabled(self, name):
        return self.enabled

    def setup_required(self, name):
        return None


def _fixture_server():
    return MCPServer(
        name="fixture",
        command="python3",
        args=[],
        cwd=".",
        # Only GREETING is the catalog's; HOST_ONLY is the backend's own env.
        env={"GREETING": "hello from the backend", "HOST_ONLY": "leak"},
        required_env_vars=["FIXTURE_TOKEN"],
        declared_env=["GREETING"],
    )


@pytest.fixture
def secrets(monkeypatch):
    values = {"FIXTURE_TOKEN": "s3cret"}

    def get_secret(key, default=None):
        return values.get(key, default)

    monkeypatch.setattr("core.secrets.get_secret", get_secret)
    monkeypatch.setattr(client_mod, "get_secret", get_secret)  # credential gate
    return values


@asynccontextmanager
async def live_runner(catalog, tmp_path, monkeypatch):  # noqa: F811
    app = create_app(TOKEN, catalog, state_dir=tmp_path / "state", switch_uid=False)
    supervisor = app.app.state.supervisor
    async with anyio.create_task_group() as tg:
        async with supervisor:
            monkeypatch.setenv("MCP_RUNNER_URL_FIXTURE", "http://mcp-runner/")
            monkeypatch.setenv("MCP_RUNNER_TOKEN", TOKEN)
            monkeypatch.setattr(
                runner_mod,
                "_http",
                lambda: httpx2.AsyncClient(
                    transport=StreamingASGITransport(app, tg),
                    headers={"Authorization": f"Bearer {runner_mod.runner_token()}"},
                ),
            )

            def no_stdio(*_a, **_k):
                raise AssertionError("stdio used in runner mode")

            monkeypatch.setattr(client_mod, "stdio_client", no_stdio)
            yield supervisor
        tg.cancel_scope.cancel()


async def _text(client, tool, **args):
    result = await client.call_tool("fixture", tool, args)
    return result["content"][0]["text"]


def test_env_push_carries_descriptor_fields_and_arg_placeholders(monkeypatch, secrets):
    descriptor = IntegrationDescriptor(
        id="fixture-vendor",
        category="test",
        mcp_server_names=("fixture",),
        fields=(
            IntegrationField("server_url"),
            IntegrationField("api_key", secret=True),
            IntegrationField("verify_ssl", value_type="bool"),
            IntegrationField("unset"),
        ),
    )
    other = IntegrationDescriptor(
        id="other", category="test", mcp_server_names=("other",)
    )
    monkeypatch.setattr(
        "core.integrations._base.descriptor.iter_descriptors",
        lambda: (descriptor, other),
    )
    monkeypatch.setattr(
        "core.integrations._base.config.resolve",
        lambda d: {
            "server_url": "https://siem.example",
            "api_key": "k3y",
            "verify_ssl": False,
            "unset": None,
        },
    )

    env = runner_mod.runner_env(_fixture_server())

    assert env == {
        "GREETING": "hello from the backend",
        "FIXTURE_TOKEN": "s3cret",
        "FIXTURE_VENDOR_SERVER_URL": "https://siem.example",
        "FIXTURE_VENDOR_API_KEY": "k3y",
        "FIXTURE_VENDOR_VERIFY_SSL": "false",
    }


async def test_connect_and_reconnect_go_through_the_runner(
    catalog, tmp_path, monkeypatch, secrets  # noqa: F811
):
    service = _Service(_fixture_server())
    client = MCPClient(service)
    async with live_runner(catalog, tmp_path, monkeypatch) as supervisor:
        assert await client.connect_to_server("fixture")
        assert {t["name"] for t in client.tools_cache["fixture"]} == {"env", "sleep"}
        assert await _text(client, "env", name="GREETING") == "hello from the backend"
        assert await _text(client, "env", name="FIXTURE_TOKEN") == "s3cret"
        assert await _text(client, "env", name="HOST_ONLY") == "<unset>"

        # Stopped under the session (another process restarted it): the call
        # that finds it gone fails, the next one PUTs it back over HTTP.
        await supervisor.delete("fixture")
        assert (await client.call_tool("fixture", "env", {"name": "GREETING"}))["error"]
        assert await _text(client, "env", name="GREETING") == "hello from the backend"
        assert supervisor.running("fixture") is not None

        # Disabled elsewhere: the reconnect re-checks and does not start it.
        await supervisor.delete("fixture")
        service.enabled = False
        await client.call_tool("fixture", "env", {"name": "GREETING"})
        result = await client.call_tool("fixture", "env", {"name": "GREETING"})
        assert result["error"]
        assert supervisor.running("fixture") is None

        await client.close_all()


async def test_close_all_leaves_the_server_and_disconnect_stops_it(
    catalog, tmp_path, monkeypatch, secrets  # noqa: F811
):
    client = MCPClient(_Service(_fixture_server()))
    other = MCPClient(_Service(_fixture_server()))
    async with live_runner(catalog, tmp_path, monkeypatch) as supervisor:
        assert await client.connect_to_server("fixture")
        assert await other.connect_to_server("fixture")
        # Closed in the order opened, which a session's own task groups forbid.
        await client.close_all()
        assert supervisor.running("fixture") is not None  # the others still use it
        assert await _text(other, "env", name="GREETING") == "hello from the backend"
        await other.close_all()

        assert await client.connect_to_server("fixture")
        assert await client.disconnect_from_server("fixture")
        assert supervisor.running("fixture") is None


async def test_probe_of_a_disabled_server_leaves_nothing_running(
    catalog, tmp_path, monkeypatch, secrets  # noqa: F811
):
    service = _Service(_fixture_server())
    service.enabled = False
    client = MCPClient(service)
    async with live_runner(catalog, tmp_path, monkeypatch) as supervisor:
        assert await client.connect_to_server(
            "fixture", persistent=False, skip_enabled_check=True
        )
        assert client.tools_cache["fixture"]
        assert supervisor.running("fixture") is None


async def test_a_refused_put_lands_on_last_errors(
    catalog, tmp_path, monkeypatch, secrets  # noqa: F811
):
    server = _fixture_server()
    server.name = "broken"  # in the runner's catalog; exits at once
    client = MCPClient(_Service(server))
    async with live_runner(catalog, tmp_path, monkeypatch):
        monkeypatch.setenv("MCP_RUNNER_URL_BROKEN", "http://mcp-runner")
        assert not await client.connect_to_server("broken")
        assert client.get_last_error("broken").startswith("MCP runner 502")
