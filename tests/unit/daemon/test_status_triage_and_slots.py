"""auto_triage_enabled and orchestrator free slots on GET /status."""

import json
from types import SimpleNamespace

import pytest

from services.daemon.config import MetricsConfig, ProcessingConfig
from services.daemon.metrics import MetricsServer
from services.daemon.processor import FindingProcessor

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    monkeypatch.setattr("services.daemon.metrics.configured_redis_url", lambda: None)


async def _status(server):
    return json.loads((await server._handle_status(None)).text)


@pytest.mark.asyncio
async def test_triage_off_and_free_slots_follow_running_count():
    processor = FindingProcessor(ProcessingConfig(auto_triage_enabled=False))
    held = {"running": 2}
    orchestrator = SimpleNamespace(
        stats={"investigations_created": 0},
        enabled=True,
        config=SimpleNamespace(max_concurrent_agents=3),
        _in_flight=lambda: 5,
        _running=lambda: held["running"],
    )
    server = MetricsServer(MetricsConfig())
    server.processor = processor
    server.orchestrator = orchestrator

    body = await _status(server)
    assert body["processor"]["auto_triage_enabled"] is False
    assert body["orchestrator"]["max_concurrent_agents"] == 3
    assert body["orchestrator"]["free_slots"] == 1
    # active_agents stays the in-flight count, not the admission gate.
    assert body["orchestrator"]["active_agents"] == 5

    held["running"] = 3
    body = await _status(server)
    assert body["orchestrator"]["free_slots"] == 0
    assert body["orchestrator"]["active_agents"] == 5

    held["running"] = 4
    body = await _status(server)
    assert body["orchestrator"]["free_slots"] == 0
