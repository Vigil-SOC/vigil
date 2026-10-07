"""Federation cursors and Kafka offsets move only once findings are stored (#1693).

The processor's input queue is in memory, so a finding that reached it but was
not yet stored is lost on a stop. Producers therefore wait for the processor's
per-item ack before advancing; the source (cursor / offset) replays the rest.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

from core.config import get_settings
from core.federation.contract import FetchResult
from core.federation.runner import FederationRunner
from core.ingestion.dedup import RedisDedupSet
from core.ingestion.kafka_config import KafkaConfig
from core.ingestion.kafka_consumer_service import KafkaConsumerService
from services.daemon.config import ProcessingConfig
from services.daemon.processor import FindingProcessor

pytestmark = pytest.mark.unit

_DOWN = ConnectionError("could not connect to server")


class _Store:
    """Stand-in IngestionService: outcomes are consumed per call, then success."""

    def __init__(self, outcomes: Optional[List[Any]] = None):
        self._outcomes = list(outcomes or [])
        self.stored: List[str] = []

    def ingest_finding(self, finding: Dict[str, Any]) -> bool:
        if self._outcomes:
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            if not outcome:
                return False
        self.stored.append(finding["finding_id"])
        return True


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")  # in-memory dedup
    get_settings.cache_clear()
    monkeypatch.setattr("services.daemon.processor._STORE_RETRY_BACKOFF", 0)
    monkeypatch.setattr("services.daemon.processor.INPUT_QUEUE_MAXSIZE", 2)
    cursors: List[Dict[str, Any]] = []
    failures: List[str] = []
    monkeypatch.setattr(
        "core.federation.runner.store.record_success",
        lambda source_id, *, cursor, **_: cursors.append(cursor),
    )
    monkeypatch.setattr(
        "core.federation.runner.store.record_failure",
        lambda source_id, error: failures.append(error),
    )
    yield cursors, failures
    get_settings.cache_clear()


def _processor(monkeypatch, store: _Store) -> FindingProcessor:
    monkeypatch.setattr(
        "core.ingestion.ingestion_service.IngestionService", lambda: store
    )
    processor = FindingProcessor(
        ProcessingConfig(
            auto_triage_enabled=False,
            auto_enrich_enabled=False,
            max_concurrent_tasks=1,
        )
    )
    processor._data_service = object()
    return processor


def _start_worker(processor: FindingProcessor) -> "asyncio.Task[None]":
    return asyncio.create_task(processor._process_worker(0, asyncio.Event()))


async def _stop(*tasks: "asyncio.Task[Any]") -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _until(predicate) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


# ---------------------------------------------------------------------------
# Federation
# ---------------------------------------------------------------------------


class _Adapter:
    name = "fake"

    def __init__(self, ids: List[str]):
        self.ids = ids
        self.cursors_seen: List[Dict[str, Any]] = []

    def is_configured(self) -> bool:
        return True

    async def fetch(self, *, since, cursor, max_items) -> FetchResult:
        self.cursors_seen.append(cursor)
        findings = [
            {"finding_id": f"f-{i}", "external_id": i, "severity": "high"}
            for i in self.ids
        ]
        return FetchResult(findings=findings, cursor={"after": self.ids[-1]})


def _runner(queue: asyncio.Queue, dedup: RedisDedupSet) -> FederationRunner:
    runner = FederationRunner(output_queue=queue)
    runner._dedup["fake"] = dedup
    return runner


ROW: Dict[str, Any] = {"max_items": 10, "cursor": {}, "min_severity": None}


@pytest.mark.asyncio
async def test_federation_stop_with_unstored_items_keeps_cursor_then_replays(
    env, monkeypatch
):
    cursors, failures = env
    adapter = _Adapter(["a", "b"])
    dedup = RedisDedupSet("federation:fake")

    # Run 1: the processor never gets to the items (the daemon is stopped).
    first = _processor(monkeypatch, _Store())
    tick = asyncio.create_task(
        _runner(first.input_queue, dedup)._do_one_tick(adapter, ROW)
    )
    await _until(lambda: first.input_queue.qsize() == 2)
    await _stop(tick)

    assert cursors == [] and failures == []
    assert await dedup.are_processed(["a", "b"]) == set()

    # Run 2: a fresh runner and processor re-read the same page.
    store = _Store()
    second = _processor(monkeypatch, store)
    worker = _start_worker(second)
    await _runner(second.input_queue, dedup)._do_one_tick(adapter, ROW)
    await _stop(worker)

    assert adapter.cursors_seen == [{}, {}]
    assert sorted(store.stored) == ["f-a", "f-b"]
    assert cursors == [{"after": "b"}]
    assert await dedup.are_processed(["a", "b"]) == {"a", "b"}


@pytest.mark.asyncio
async def test_federation_failed_store_holds_cursor_and_retries_only_that_item(
    env, monkeypatch
):
    cursors, failures = env
    adapter = _Adapter(["a", "b", "c"])
    dedup = RedisDedupSet("federation:fake")
    # a stores; b fails every attempt; c stores.
    store = _Store([True, _DOWN, _DOWN, _DOWN, True])
    processor = _processor(monkeypatch, store)
    worker = _start_worker(processor)
    runner = _runner(processor.input_queue, dedup)

    await runner._do_one_tick(adapter, ROW)
    assert store.stored == ["f-a", "f-c"]
    assert cursors == []
    assert len(failures) == 1 and "1 finding(s) not stored" in failures[0]
    assert await dedup.are_processed(["a", "b", "c"]) == {"a", "c"}

    await runner._do_one_tick(adapter, ROW)
    await _stop(worker)
    assert store.stored == ["f-a", "f-c", "f-b"]  # each stored exactly once
    assert cursors == [{"after": "c"}]


@pytest.mark.asyncio
async def test_a_full_queue_blocks_the_federation_producer(env, monkeypatch):
    cursors, _ = env
    adapter = _Adapter(["a", "b", "c"])
    store = _Store()
    processor = _processor(monkeypatch, store)
    assert processor.input_queue.maxsize == 2
    tick = asyncio.create_task(
        _runner(processor.input_queue, RedisDedupSet("federation:fake"))._do_one_tick(
            adapter, ROW
        )
    )

    await _until(lambda: processor.input_queue.full())
    await asyncio.sleep(0.05)
    assert (
        not tick.done() and processor.input_queue.qsize() == 2
    )  # c waits, not dropped

    worker = _start_worker(processor)
    await asyncio.wait_for(tick, timeout=5)
    await _stop(worker)
    assert sorted(store.stored) == ["f-a", "f-b", "f-c"]
    assert cursors == [{"after": "c"}]


# ---------------------------------------------------------------------------
# Kafka
# ---------------------------------------------------------------------------


class _Topic:
    topic = "security.findings"


class _Record:
    def __init__(self, offset: int):
        self.offset = offset
        self.value = json.dumps({"finding_id": f"k-{offset}"}).encode()


class _Consumer:
    """Serves one batch, then idles; records commits and seeks."""

    def __init__(self, tp: _Topic, offsets: List[int]):
        self._batch = {tp: [_Record(o) for o in offsets]}
        self.polls = 0
        self.commits: List[Dict[Any, int]] = []
        self.seeks: List[int] = []

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def getmany(self, timeout_ms: int = 1000):
        self.polls += 1
        if self.polls == 1:
            return self._batch
        await asyncio.sleep(0.01)
        return {}

    async def commit(self, offsets: Dict[Any, int]) -> None:
        self.commits.append(offsets)

    def seek(self, tp: Any, offset: int) -> None:
        self.seeks.append(offset)
        self.polls = 0  # the same records are served again after a rewind


def _kafka(queue, dedup, consumer, monkeypatch) -> KafkaConsumerService:
    service = KafkaConsumerService(
        KafkaConfig(
            enabled=True,
            bootstrap_servers="localhost:9092",
            consumer_group="test",
            topics=["security.findings"],
        ),
        queue,
        dedup,
    )

    async def _build():
        return consumer

    monkeypatch.setattr(service, "_build_consumer", _build)
    return service


@pytest.mark.asyncio
async def test_kafka_stop_with_unstored_items_commits_nothing_then_replays(
    env, monkeypatch
):
    dedup = RedisDedupSet("kafka")
    tp = _Topic()

    first = _processor(monkeypatch, _Store())
    consumer1 = _Consumer(tp, [7, 8])
    run1 = asyncio.create_task(
        _kafka(first.input_queue, dedup, consumer1, monkeypatch).run(asyncio.Event())
    )
    await _until(lambda: first.input_queue.qsize() == 2)
    await _stop(run1)

    assert consumer1.commits == []
    assert await dedup.are_processed(["k-7", "k-8"]) == set()

    store = _Store()
    second = _processor(monkeypatch, store)
    worker = _start_worker(second)
    consumer2 = _Consumer(tp, [7, 8])
    shutdown = asyncio.Event()
    run2 = asyncio.create_task(
        _kafka(second.input_queue, dedup, consumer2, monkeypatch).run(shutdown)
    )
    await _until(lambda: consumer2.commits)
    shutdown.set()
    await asyncio.wait_for(run2, timeout=5)
    await _stop(worker)

    assert sorted(store.stored) == ["k-7", "k-8"]
    assert consumer2.commits == [{tp: 9}]  # past the last stored offset only


@pytest.mark.asyncio
async def test_kafka_failed_store_rewinds_instead_of_committing(env, monkeypatch):
    monkeypatch.setattr(
        "core.ingestion.kafka_consumer_service._RETRY_BACKOFF_SECONDS", 0
    )
    dedup = RedisDedupSet("kafka")
    tp = _Topic()
    store = _Store([True, _DOWN, _DOWN, _DOWN])  # k-7 stores, k-8 fails once
    processor = _processor(monkeypatch, store)
    worker = _start_worker(processor)
    consumer = _Consumer(tp, [7, 8])
    shutdown = asyncio.Event()
    run = asyncio.create_task(
        _kafka(processor.input_queue, dedup, consumer, monkeypatch).run(shutdown)
    )
    await _until(lambda: consumer.commits)
    shutdown.set()
    await asyncio.wait_for(run, timeout=5)
    await _stop(worker)

    assert consumer.seeks == [7]  # rewound to the batch start, not committed
    assert store.stored == ["k-7", "k-8"]  # k-7 once; the replay only stored k-8
    assert consumer.commits == [{tp: 9}]
