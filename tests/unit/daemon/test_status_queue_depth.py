"""Queue depths, legacy poll times, and Redis depths on GET /status."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from core.time import utcnow
from services.daemon.config import MetricsConfig, PollingConfig, ProcessingConfig
from services.daemon.metrics import MetricsServer
from services.daemon.poller import DataPoller
from services.daemon.processor import INPUT_QUEUE_MAXSIZE, FindingProcessor

pytestmark = pytest.mark.unit

_LEGACY_SOURCES = (
    "splunk",
    "crowdstrike",
    "azure_sentinel",
    "aws_security_hub",
    "microsoft_defender",
    "elastic",
    "opensearch",
)


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    monkeypatch.setattr("services.daemon.metrics.configured_redis_url", lambda: None)


async def _status(server):
    return json.loads((await server._handle_status(None)).text)


@pytest.mark.asyncio
async def test_queue_depths_follow_the_queues():
    processor = FindingProcessor(ProcessingConfig(auto_enrich_enabled=False))
    responder = SimpleNamespace(stats={}, input_queue=asyncio.Queue())
    for n in range(3):
        processor.input_queue.put_nowait({"n": n})
    for n in range(2):
        responder.input_queue.put_nowait({"n": n})
    processor._enrich_tasks.add(object())

    server = MetricsServer(MetricsConfig())
    server.processor = processor
    server.responder = responder
    body = await _status(server)
    assert body["processor"]["queue_depth"] == 3
    assert body["processor"]["queue_max"] == INPUT_QUEUE_MAXSIZE
    assert body["processor"]["processed"] == 0
    assert body["processor"]["enrich_inflight"] == 1
    assert body["responder"]["queue_depth"] == 2

    for _ in range(3):
        processor.input_queue.get_nowait()
    responder.input_queue.get_nowait()
    responder.input_queue.get_nowait()
    body = await _status(server)
    assert body["processor"]["queue_depth"] == 0
    assert body["responder"]["queue_depth"] == 0


@pytest.mark.asyncio
async def test_missing_components_emit_zeros_and_health_stays_put():
    server = MetricsServer(MetricsConfig())
    body = await _status(server)
    assert body["processor"]["queue_depth"] == 0
    assert body["processor"]["queue_max"] == 0
    assert body["processor"]["enrich_inflight"] == 0
    assert body["processor"]["enrich_breaker_tripped"] is False
    assert body["processor"]["enrich_paused_until"] is None
    assert body["processor"]["auto_triage_enabled"] is False
    assert body["responder"]["queue_depth"] == 0
    assert body["orchestrator"] == {"max_concurrent_agents": 0, "free_slots": 0}
    assert body["federation"] == {"polls": 0, "findings": 0, "errors": 0, "dropped": 0}
    assert body["llm_queue_depth"] is None
    assert body["agent_runs_depth"] is None
    sources = body["poller"]["sources"]
    assert list(sources) == list(_LEGACY_SOURCES)
    assert all(row["last_poll_time"] is None for row in sources.values())
    assert "webhook" not in sources

    health = json.loads((await server._handle_health(None)).text)
    assert "federation" not in health
    assert "llm_queue_depth" not in health
    assert "queue_depth" not in json.dumps(health["components"])


@pytest.mark.asyncio
async def test_legacy_poll_times_come_from_poll_state():
    poller = DataPoller(PollingConfig())
    stamped = utcnow() - timedelta(minutes=5)
    poller._splunk_state.last_poll_time = stamped
    server = MetricsServer(MetricsConfig())
    server.poller = poller
    sources = (await _status(server))["poller"]["sources"]
    assert sources["splunk"]["last_poll_time"] == stamped.isoformat()
    assert sources["elastic"]["last_poll_time"] is None
    assert sources["opensearch"]["last_poll_time"] is None


class _Redis:
    def __init__(self, llm, runs):
        self._llm = llm
        self._runs = runs
        self.calls = []

    def zcard(self, key):
        self.calls.append(("zcard", key))
        return self._llm

    def llen(self, key):
        self.calls.append(("llen", key))
        return self._runs

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


@pytest.mark.asyncio
async def test_redis_depths_are_zcard_and_llen(monkeypatch):
    client = _Redis(4, 9)

    def _open(**kwargs):
        assert kwargs["socket_timeout"] == 3
        assert kwargs["socket_connect_timeout"] == 3
        return client

    monkeypatch.setattr(
        "services.daemon.metrics.configured_redis_url", lambda: "redis://cache:6379/0"
    )
    monkeypatch.setattr("services.daemon.metrics.open_sync_redis", _open)
    body = await _status(MetricsServer(MetricsConfig()))
    assert body["llm_queue_depth"] == 4
    assert body["agent_runs_depth"] == 9
    assert client.calls == [("zcard", "arq:llm"), ("llen", "bull:agent-runs:wait")]


@pytest.mark.asyncio
async def test_redis_depths_null_when_the_command_fails(monkeypatch):
    monkeypatch.setattr(
        "services.daemon.metrics.configured_redis_url", lambda: "redis://cache:6379/0"
    )

    def _open(**_kwargs):
        raise ConnectionError("refused")

    monkeypatch.setattr("services.daemon.metrics.open_sync_redis", _open)
    body = await _status(MetricsServer(MetricsConfig()))
    assert body["llm_queue_depth"] is None
    assert body["agent_runs_depth"] is None


@pytest.mark.asyncio
async def test_redis_depths_null_when_no_url_is_configured(monkeypatch):
    def _open(**_kwargs):
        raise AssertionError("status dialed Redis without a configured URL")

    monkeypatch.setattr("services.daemon.metrics.open_sync_redis", _open)
    body = await _status(MetricsServer(MetricsConfig()))
    assert body["llm_queue_depth"] is None
    assert body["agent_runs_depth"] is None
