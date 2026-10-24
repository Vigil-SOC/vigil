import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import ClientConnectorError, ClientSession

from services.daemon.config import DaemonConfig
from services.daemon.main import SOCDaemon
from services.daemon.metrics import MetricsServer

pytestmark = pytest.mark.unit

COMPONENTS = [
    ("poller", "DataPoller"),
    ("kafka_ingestor", "KafkaIngestor"),
    ("processor", "FindingProcessor"),
    ("responder", "AutonomousResponder"),
    ("scheduler", "TaskScheduler"),
    ("orchestrator", "Orchestrator"),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Component:
    enabled = True
    stats: dict = {}

    def __init__(self, *args, **kwargs):
        self.input_queue = asyncio.Queue()

    def _in_flight(self):
        return 0

    def set_output_queue(self, queue): ...

    def set_response_queue(self, queue): ...

    def set_processor_queue(self, queue): ...

    async def run(self, shutdown_event):
        await shutdown_event.wait()


async def _status(session, url):
    try:
        async with session.get(url) as resp:
            return resp.status
    except ClientConnectorError:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("metrics_enabled", [True, False])
async def test_health_served_and_prometheus_follows_flag(metrics_enabled):
    health_port, metrics_port = _free_port(), _free_port()
    config = DaemonConfig()
    config.metrics.enabled = metrics_enabled
    patches = [
        patch(f"services.daemon.{module}.{name}", _Component)
        for module, name in COMPONENTS
    ] + [
        patch.object(SOCDaemon, "_setup_signal_handlers"),
        patch("core.storage.connection.get_db_manager"),
        patch("core.integrations.mcp.client.build_mcp_client"),
        patch("core.integrations.mcp.client.set_process_mcp_client"),
        patch("core.response.approval_service.ApprovalService"),
        patch("core.response.autonomous_response_service.AutonomousResponseService"),
        patch(
            "services.daemon.metrics.get_settings",
            return_value=SimpleNamespace(daemon_bind_host="127.0.0.1"),
        ),
        patch.object(MetricsServer, "health_port", health_port),
        patch.object(MetricsServer, "metrics_port", metrics_port),
    ]
    for p in patches:
        p.start()
    try:
        daemon = SOCDaemon(config)
        task = asyncio.create_task(daemon.run())
        async with ClientSession() as session:
            for _ in range(100):
                health = await _status(
                    session, f"http://127.0.0.1:{health_port}/health"
                )
                if health is not None:
                    break
                await asyncio.sleep(0.02)
            status = await _status(session, f"http://127.0.0.1:{health_port}/status")
            metrics = await _status(session, f"http://127.0.0.1:{metrics_port}/metrics")
        daemon._shutdown_event.set()
        await task
    finally:
        for p in patches:
            p.stop()

    assert health == 200
    assert status == 200
    assert metrics == (200 if metrics_enabled else None)
