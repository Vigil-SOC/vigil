"""Daemon listeners bind to settings.daemon_bind_host (#1683)."""

import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import ClientSession, web

from services.daemon.config import MetricsConfig, PollingConfig
from services.daemon.metrics import MetricsServer
from services.daemon.poller import DataPoller


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.asyncio
async def test_metrics_server_binds_configured_host():
    ports = (_free_port(), _free_port())
    server = MetricsServer(MetricsConfig())
    shutdown = asyncio.Event()
    with patch(
        "services.daemon.metrics.get_settings",
        return_value=SimpleNamespace(daemon_bind_host="127.0.0.1"),
    ), patch.object(MetricsServer, "health_port", ports[0]), patch.object(
        MetricsServer, "metrics_port", ports[1]
    ), patch(
        "services.daemon.metrics.web.TCPSite", wraps=web.TCPSite
    ) as site:
        task = asyncio.create_task(server.run(shutdown))
        for _ in range(100):
            if site.call_count == 2:
                break
            await asyncio.sleep(0.02)
        shutdown.set()
        await task
    assert [c.args[1] for c in site.call_args_list] == ["127.0.0.1", "127.0.0.1"]


@pytest.mark.asyncio
async def test_webhook_server_binds_configured_host_and_health_has_no_stats():
    port = _free_port()
    poller = DataPoller(PollingConfig(webhook_port=port))
    shutdown = asyncio.Event()
    with patch(
        "services.daemon.poller.get_settings",
        return_value=SimpleNamespace(daemon_bind_host="127.0.0.1"),
    ), patch("aiohttp.web.TCPSite", wraps=web.TCPSite) as site:
        task = asyncio.create_task(poller._run_webhook_server(shutdown))
        body = None
        async with ClientSession() as session:
            for _ in range(100):
                try:
                    async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                        body = await resp.json()
                    break
                except OSError:
                    await asyncio.sleep(0.02)
        shutdown.set()
        await task
    assert site.call_args.args[1] == "127.0.0.1"
    assert body == {"status": "healthy"}
