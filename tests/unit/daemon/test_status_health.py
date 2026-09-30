"""/status shows Federation and the hand-off; /health shows Federation's setup."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from services.daemon.config import MetricsConfig, PollingConfig
from services.daemon.metrics import MetricsServer
from services.daemon.poller import DataPoller

pytestmark = pytest.mark.unit


def _server():
    server = MetricsServer(MetricsConfig())
    server.poller = DataPoller(PollingConfig())
    queue: asyncio.Queue = asyncio.Queue(maxsize=5)
    queue.put_nowait("waiting")
    server.processor = SimpleNamespace(stats={"processed": 3}, input_queue=queue)
    return server


def test_status_carries_federation_counts_live_and_the_queue_depth():
    server = _server()
    server.poller._federation.stats["polls"] = 7

    metrics = server._collect_metrics()

    assert metrics["poller"]["federation"]["polls"] == 7
    assert metrics["processor"]["processed"] == 3
    assert metrics["processor"]["queue_depth"] == 1
    assert metrics["processor"]["queue_maxsize"] == 5
    assert "handoff_full_waits" in metrics["processor"]


@pytest.mark.parametrize(
    "fed, component",
    [
        ({}, "degraded"),
        ({"setup_ok": False, "adapters": 6}, "degraded"),
        ({"setup_ok": True, "adapters": 0}, "degraded"),
        ({"setup_ok": True, "adapters": 6}, "running"),
    ],
    ids=["booting", "setup-retrying", "no-adapters", "ready"],
)
@pytest.mark.asyncio
async def test_health_reports_federation_without_failing_the_probes(fed, component):
    server = _server()
    server.poller._federation.stats.update(fed)

    resp = await server._handle_health(None)

    assert resp.status == 200
    assert json.loads(resp.text)["components"]["federation"] == component
