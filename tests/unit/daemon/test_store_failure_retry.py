"""A store that fails must not leave the finding marked processed.

The processor retries ``_store_finding`` in place. On give-up it forgets the
dedup key that was marked at enqueue, on the same ``RedisDedupSet`` instance.

Splunk, CrowdStrike, and Elastic re-read a lookback of
``max(interval // 60 + 1, 5)`` minutes, so an unmarked id is queued again.
Webhook, federation, and Kafka do not: the HTTP response, the cursor, and
the offset have already moved.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest
from aiohttp import ClientConnectorError, ClientSession

from core.config import get_settings
from core.federation.contract import FetchResult
from core.federation.runner import FederationRunner
from core.ingestion.dedup import RedisDedupSet
from core.ingestion.kafka_config import KafkaConfig
from core.ingestion.kafka_consumer_service import KafkaConsumerService
from services.daemon.config import PollingConfig, ProcessingConfig
from services.daemon.poller import DataPoller
from services.daemon.processor import FindingProcessor, _STORE_ATTEMPTS

pytestmark = pytest.mark.unit

_FIXED = datetime(2026, 10, 1, 12, 0, 0)


class _Ingest:
    def __init__(self, results: List[Any]):
        self._results = list(results)
        self.calls = 0
        self.finding_ids: List[str] = []

    def ingest_finding(self, finding: Dict[str, Any]) -> bool:
        self.calls += 1
        self.finding_ids.append(finding.get("finding_id"))
        outcome = self._results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return bool(outcome)


def _dead_redis(monkeypatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    get_settings.cache_clear()


def _poller(monkeypatch) -> DataPoller:
    _dead_redis(monkeypatch)
    poller = DataPoller(PollingConfig(webhook_token="test-token", webhook_port=18781))
    poller._federation.is_active_for = lambda source_id: False
    poller.set_output_queue(asyncio.Queue())
    return poller


def _processor(monkeypatch) -> FindingProcessor:
    monkeypatch.setattr("services.daemon.processor._STORE_RETRY_BACKOFF", 0)
    processor = FindingProcessor(
        ProcessingConfig(
            auto_triage_enabled=False,
            auto_enrich_enabled=False,
            max_concurrent_tasks=1,
        )
    )
    processor._data_service = object()
    return processor


def _patch_ingest(monkeypatch, ingest: _Ingest) -> None:
    monkeypatch.setattr(
        "core.ingestion.ingestion_service.IngestionService",
        lambda: ingest,
    )


async def _drain(processor: FindingProcessor, queue: asyncio.Queue) -> Dict[str, Any]:
    item = queue.get_nowait()
    await processor._process_item(item)
    return item


class _Splunk:
    def __init__(self, events: List[Dict[str, Any]]):
        self.events = events
        self.queries: List[Dict[str, Any]] = []

    def search(self, **kwargs: Any) -> List[Dict[str, Any]]:
        self.queries.append(kwargs)
        return list(self.events)


class _CrowdStrike:
    def __init__(self, detections: List[Dict[str, Any]]):
        self.detections = detections
        self.queries: List[str] = []

    def get_detections(self, filter_query: str, limit: int) -> List[Dict[str, Any]]:
        self.queries.append(filter_query)
        return list(self.detections)


class _Elastic:
    def __init__(self, alerts: List[Dict[str, Any]]):
        self.alerts = alerts
        self.start_times: List[Any] = []

    async def fetch_alerts(self, start_time: Any, limit: int) -> List[Dict[str, Any]]:
        self.start_times.append(start_time)
        return list(self.alerts)

    def transform_alert_to_finding(self, alert: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "finding_id": f"elastic-{alert['id']}",
            "severity": "high",
            "data_source": "elastic",
        }


def _down() -> List[Exception]:
    return [ConnectionError("could not connect to server: Connection refused")] * (
        _STORE_ATTEMPTS
    )


@pytest.mark.asyncio
async def test_one_store_failure_then_success_keeps_the_key(monkeypatch):
    poller = _poller(monkeypatch)
    processor = _processor(monkeypatch)
    poller._splunk_service = _Splunk(
        [{"_cd": "42:1001", "search_name": "rule", "urgency": "high"}]
    )
    ingest = _Ingest(
        [ConnectionError("could not connect to server: Connection refused"), True]
    )
    _patch_ingest(monkeypatch, ingest)

    await poller._poll_splunk()
    item = await _drain(processor, poller._output_queue)

    finding_id = "splunk-42:1001"
    assert item["dedup"] is poller._splunk_dedup
    assert item["dedup_key"] == finding_id
    assert ingest.calls == 2
    assert ingest.finding_ids == [finding_id, finding_id]
    assert processor.stats["processed"] == 1
    assert processor.stats["store_dropped"] == 0
    assert await poller._splunk_dedup.is_processed(finding_id) is True
    assert poller._output_queue.empty()


@pytest.mark.asyncio
async def test_a_store_that_keeps_failing_is_dropped_and_splunk_requeues(
    monkeypatch, caplog
):
    poller = _poller(monkeypatch)
    processor = _processor(monkeypatch)
    event = {"_cd": "42:1001", "search_name": "rule", "urgency": "high"}
    poller._splunk_service = _Splunk([event])
    ingest = _Ingest(_down())
    _patch_ingest(monkeypatch, ingest)

    await poller._poll_splunk()
    # Default Splunk interval is 300s, so the window is max(300 // 60 + 1, 5) minutes.
    assert poller._splunk_service.queries[0]["earliest_time"] == "-6m"
    assert await poller._splunk_dedup.is_processed("splunk-42:1001") is True

    caplog.set_level(logging.ERROR)
    await _drain(processor, poller._output_queue)

    assert ingest.calls == _STORE_ATTEMPTS
    assert processor.stats["processed"] == 0
    assert processor.stats["store_dropped"] == 1
    assert processor.stats["errors"] == 0
    assert "Dropped finding splunk-42:1001" in caplog.text
    assert await poller._splunk_dedup.is_processed("splunk-42:1001") is False

    await poller._poll_splunk()
    again = poller._output_queue.get_nowait()
    assert again["data"]["finding_id"] == "splunk-42:1001"
    assert again["dedup"] is poller._splunk_dedup
    assert await poller._splunk_dedup.is_processed("splunk-42:1001") is True


@pytest.mark.asyncio
async def test_crowdstrike_and_elastic_requeue_inside_their_lookback(monkeypatch):
    """Both re-read max(interval // 60 + 1, 5) minutes, so an unmarked id returns."""
    poller = _poller(monkeypatch)
    processor = _processor(monkeypatch)
    ingest = _Ingest(_down() + _down())
    _patch_ingest(monkeypatch, ingest)

    crowdstrike = _CrowdStrike(
        [{"detection_id": "det-1", "max_severity_displayname": "High"}]
    )
    elastic = _Elastic([{"id": "a1"}])
    poller._crowdstrike_service = crowdstrike
    poller._elastic_service = elastic

    with patch("services.daemon.poller.utcnow", return_value=_FIXED):
        await poller._poll_crowdstrike()
        await _drain(processor, poller._output_queue)
        await poller._poll_crowdstrike()
        requeued_cs = poller._output_queue.get_nowait()

        await poller._poll_elastic()
        await _drain(processor, poller._output_queue)
        await poller._poll_elastic()
        requeued_elastic = poller._output_queue.get_nowait()

    # Default intervals: CrowdStrike 60s -> 5 minutes; Elastic shares Splunk's
    # 300s -> 6 minutes.
    assert crowdstrike.queries[0] == "created_timestamp:>='2026-10-01T11:55:00Z'"
    assert elastic.start_times[0] == _FIXED - timedelta(minutes=6)
    assert requeued_cs["data"]["finding_id"] == "cs-det-1"
    assert requeued_elastic["data"]["finding_id"] == "elastic-a1"
    assert poller._output_queue.empty()
    assert processor.stats["store_dropped"] == 2
    assert await poller._crowdstrike_dedup.is_processed("cs-det-1") is True
    assert await poller._elastic_dedup.is_processed("elastic-a1") is True


@pytest.mark.asyncio
async def test_missing_data_service_drops_and_unmarks(monkeypatch, caplog):
    _dead_redis(monkeypatch)
    monkeypatch.setattr("services.daemon.processor._STORE_RETRY_BACKOFF", 0)
    processor = FindingProcessor(
        ProcessingConfig(auto_triage_enabled=False, auto_enrich_enabled=False)
    )
    dedup = RedisDedupSet("unit-missing-db")
    await dedup.mark_processed("f-missing")

    caplog.set_level(logging.ERROR)
    await processor._process_finding(
        {"finding_id": "f-missing"},
        source="splunk",
        dedup=dedup,
        dedup_key="f-missing",
    )

    assert processor.stats["processed"] == 0
    assert processor.stats["store_dropped"] == 1
    assert "Dropped finding f-missing" in caplog.text
    assert await dedup.is_processed("f-missing") is False


@pytest.mark.asyncio
async def test_a_probe_without_a_dedup_key_is_dropped_quietly(monkeypatch):
    processor = _processor(monkeypatch)
    processor._data_service = None
    await processor._process_item(
        {
            "type": "finding",
            "source": "probe",
            "data": {"finding_id": "probe:canary:2026-10-01"},
        }
    )
    assert processor.stats["store_dropped"] == 1
    assert processor.stats["processed"] == 0


class _Msg:
    def __init__(self, value: bytes):
        self.value = value


class _Topic:
    def __init__(self, topic: str):
        self.topic = topic


class _CursorAdapter:
    name = "fake"

    def __init__(self):
        self.fetch_calls: List[Dict[str, Any]] = []

    def is_configured(self) -> bool:
        return True

    async def fetch(
        self, *, since: Optional[datetime], cursor: Dict[str, Any], max_items: int
    ) -> FetchResult:
        self.fetch_calls.append(cursor)
        if cursor.get("done"):
            return FetchResult(findings=[], cursor=cursor)
        return FetchResult(
            findings=[
                {
                    "finding_id": "f-1",
                    "external_id": "ext-1",
                    "severity": "high",
                    "data_source": "fake",
                }
            ],
            cursor={"done": True},
        )


@pytest.mark.asyncio
async def test_webhook_federation_and_kafka_do_not_redeliver(monkeypatch, caplog):
    """The 200, the cursor, and the offset move before the store is attempted."""
    _dead_redis(monkeypatch)
    processor = _processor(monkeypatch)
    ingest = _Ingest(_down() + _down() + _down())
    _patch_ingest(monkeypatch, ingest)
    caplog.set_level(logging.ERROR)

    # Webhook: the handler has already answered 200 by the time we store.
    poller = _poller(monkeypatch)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        poller.config.webhook_port = sock.getsockname()[1]
    disabled = set()

    class _Config:
        def get_disabled_integration_ids(self):
            return disabled

    shutdown = asyncio.Event()
    server = asyncio.create_task(poller._run_webhook_server(shutdown))
    try:
        with patch(
            "core.storage.config_service.get_config_service",
            return_value=_Config(),
        ):
            async with ClientSession() as session:
                response = await _post_when_up(session, poller.config.webhook_port)
                body = await response.json()
                status = response.status
        assert status == 200
        assert body == {"status": "ok", "ingested": 1}
        assert await poller._webhook_dedup.is_processed("wh-1") is True

        await _drain(processor, poller._output_queue)
        assert processor.stats["store_dropped"] == 1
        assert await poller._webhook_dedup.is_processed("wh-1") is False
        # Nothing in the webhook path asks the sender to try again.
        assert poller._output_queue.empty()
    finally:
        shutdown.set()
        await server

    # Federation: record_success advances the cursor even though the store
    # later fails, and the next tick's window does not contain the finding.
    queue: asyncio.Queue = asyncio.Queue()
    runner = FederationRunner(output_queue=queue)
    adapter = _CursorAdapter()
    runner._dedup[adapter.name] = RedisDedupSet("federation:fake")
    stored: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        "core.federation.runner.store.record_success",
        lambda source_id, *, cursor, **_: stored.append(cursor),
    )
    monkeypatch.setattr(
        "core.federation.runner.store.record_failure",
        lambda *args, **kwargs: None,
    )
    row: Dict[str, Any] = {"max_items": 100, "cursor": {}, "min_severity": None}
    await runner._do_one_tick(adapter, row)
    item = await _drain(processor, queue)
    assert item["dedup_key"] == "ext-1"
    assert item["dedup"] is runner._dedup["fake"]
    assert stored == [{"done": True}]
    assert await runner._dedup["fake"].is_processed("ext-1") is False
    assert await runner._dedup["fake"].is_processed("f-1") is False

    await runner._do_one_tick(adapter, {**row, "cursor": stored[0]})
    assert adapter.fetch_calls[1] == {"done": True}
    assert queue.empty()

    # No output queue: the put never happens, so the key stays unmarked.
    unmarked = FederationRunner(output_queue=None)
    unmarked._dedup[adapter.name] = RedisDedupSet("federation:nowhere")
    await unmarked._do_one_tick(adapter, row)
    assert await unmarked._dedup["fake"].is_processed("ext-1") is False
    assert unmarked.stats["findings"] == 0

    # Kafka: the offset is committed when the message is handled, before
    # the processor stores. The next poll is empty.
    kqueue: asyncio.Queue = asyncio.Queue()
    kdedup = RedisDedupSet("kafka-drop")
    consumer_shutdown = asyncio.Event()

    class _Consumer:
        def __init__(self):
            self.commits = 0
            self.polls = 0

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        async def commit(self) -> None:
            self.commits += 1

        async def getmany(self, timeout_ms: int = 1000):
            self.polls += 1
            if self.polls == 1:
                payload = json.dumps({"finding_id": "k-1"}).encode()
                return {_Topic("security.findings"): [_Msg(payload)]}
            consumer_shutdown.set()
            return {}

    fake = _Consumer()
    service = KafkaConsumerService(
        KafkaConfig(
            enabled=True,
            bootstrap_servers="localhost:9092",
            consumer_group="test",
            topics=["security.findings"],
        ),
        kqueue,
        kdedup,
    )

    async def _build():
        return fake

    monkeypatch.setattr(service, "_build_consumer", _build)
    await service.run(consumer_shutdown)

    assert fake.commits == 1
    assert fake.polls == 2
    assert service.stats["messages_enqueued"] == 1
    await _drain(processor, kqueue)
    assert await kdedup.is_processed("k-1") is False
    assert kqueue.empty()
    assert service.stats["messages_enqueued"] == 1
    assert processor.stats["processed"] == 0


async def _post_when_up(session: ClientSession, port: int):
    url = f"http://127.0.0.1:{port}/ingest"
    headers = {"Authorization": "Bearer test-token"}
    body = {"finding_id": "wh-1", "severity": "high"}
    last_error: Optional[BaseException] = None
    for _ in range(50):
        try:
            return await session.post(url, json=body, headers=headers)
        except (ClientConnectorError, ConnectionError, OSError) as exc:
            last_error = exc
            await asyncio.sleep(0.05)
    raise RuntimeError(f"webhook server did not accept a connection: {last_error}")
