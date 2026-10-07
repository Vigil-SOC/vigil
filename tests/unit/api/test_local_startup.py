import asyncio
from unittest.mock import MagicMock

import pytest

from services.api.main import (
    _connect_enabled_mcp_servers,
    _mcp_auto_connect_enabled,
)


def test_mcp_auto_connect_is_off_by_default_in_dev(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.delenv("MCP_AUTO_CONNECT_ON_STARTUP", raising=False)

    assert _mcp_auto_connect_enabled() is False


def test_mcp_auto_connect_can_be_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("MCP_AUTO_CONNECT_ON_STARTUP", "true")

    assert _mcp_auto_connect_enabled() is True


def _client(servers, enabled, connect):
    client = MagicMock()
    client.mcp_service.list_servers.return_value = list(servers)
    client.mcp_service.is_server_enabled.side_effect = lambda name: enabled[name]
    client.connect_to_server = connect
    client.get_missing_credentials.return_value = None
    return client


@pytest.mark.asyncio
async def test_enabled_mcp_servers_connect_concurrently():
    started = []
    gate = asyncio.Event()

    persistents = []

    async def connect(server_name, persistent=True):
        started.append(server_name)
        persistents.append(persistent)
        if len(started) >= 2:
            gate.set()
        await gate.wait()
        return True

    client = _client(
        ["alpha", "beta", "disabled"],
        {"alpha": True, "beta": True, "disabled": False},
        connect,
    )

    count = await asyncio.wait_for(_connect_enabled_mcp_servers(client), timeout=1.0)

    assert count == 2
    assert set(started) == {"alpha", "beta"}
    assert persistents == [True, True]


@pytest.mark.asyncio
async def test_one_mcp_connect_failure_does_not_block_the_rest():
    attempted = []
    gate = asyncio.Event()

    async def connect(server_name, persistent=True):
        attempted.append(server_name)
        if len(attempted) >= 3:
            gate.set()
        await gate.wait()
        if server_name == "bad":
            raise RuntimeError("spawn failed")
        return True

    client = _client(
        ["good", "bad", "also-good"],
        {"good": True, "bad": True, "also-good": True},
        connect,
    )

    count = await asyncio.wait_for(_connect_enabled_mcp_servers(client), timeout=1.0)

    assert set(attempted) == {"good", "bad", "also-good"}
    assert count == 2


@pytest.mark.asyncio
async def test_dormant_mcp_server_is_not_counted_as_connected():
    async def connect(server_name, persistent=True):
        return False

    client = _client(["dormant"], {"dormant": True}, connect)
    client.get_missing_credentials.return_value = ["API_KEY"]

    count = await _connect_enabled_mcp_servers(client)

    assert count == 0
