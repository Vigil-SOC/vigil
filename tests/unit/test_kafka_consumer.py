"""Unit tests for KafkaConsumerService message handling.

These tests exercise the _handle_message path directly (no real Kafka
broker, no real Redis) so they can run in CI without infrastructure.
The aiokafka dependency is intentionally not imported at module scope —
the consumer only imports it lazily inside _build_consumer.
"""

from __future__ import annotations

import asyncio
import json
import pytest

from daemon.config import KafkaConfig
from daemon.dedup import RedisDedupSet
from services.kafka_consumer_service import KafkaConsumerService


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_service(monkeypatch, topics=None):
    # Force dedup into in-memory fallback
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    cfg = KafkaConfig(
        enabled=True,
        bootstrap_servers="localhost:9092",
        consumer_group="test",
        topics=topics or ["security.findings"],
    )
    queue: asyncio.Queue = asyncio.Queue()
    dedup = RedisDedupSet("test-kafka", max_size=32)
    svc = KafkaConsumerService(cfg, queue, dedup)
    return svc, queue


class _Msg:
    """Stand-in for aiokafka's ConsumerRecord."""

    def __init__(self, value):
        self.value = value


class TestKafkaMessageHandling:
    def test_valid_json_message_enqueued(self, monkeypatch):
        svc, queue = _make_service(monkeypatch)
        payload = {"finding_id": "f-1", "severity": "high"}
        msg = _Msg(json.dumps(payload).encode("utf-8"))

        async def go():
            await svc._handle_message("security.findings", msg)
            return await queue.get()

        item = _run(go())
        assert item["type"] == "finding"
        assert item["source"] == "kafka:security.findings"
        assert item["data"]["finding_id"] == "f-1"
        assert item["data"]["data_source"] == "kafka:security.findings"
        assert svc.stats["messages_enqueued"] == 1
        assert svc.stats["messages_consumed"] == 1
        assert svc.stats["decode_errors"] == 0

    def test_producer_data_source_is_preserved(self, monkeypatch):
        svc, queue = _make_service(monkeypatch)
        payload = {"finding_id": "f-2", "data_source": "custom-edr"}
        msg = _Msg(json.dumps(payload).encode("utf-8"))

        async def go():
            await svc._handle_message("sec.t", msg)
            return await queue.get()

        item = _run(go())
        assert item["data"]["data_source"] == "custom-edr"

    def test_malformed_json_skipped(self, monkeypatch):
        svc, queue = _make_service(monkeypatch)
        msg = _Msg(b"{not valid json")

        async def go():
            await svc._handle_message("sec.t", msg)
            return queue.qsize()

        assert _run(go()) == 0
        assert svc.stats["decode_errors"] == 1
        assert svc.stats["messages_enqueued"] == 0

    def test_non_object_payload_skipped(self, monkeypatch):
        svc, queue = _make_service(monkeypatch)
        msg = _Msg(json.dumps([1, 2, 3]).encode("utf-8"))

        async def go():
            await svc._handle_message("sec.t", msg)
            return queue.qsize()

        assert _run(go()) == 0
        assert svc.stats["decode_errors"] == 1

    def test_missing_finding_id_skipped(self, monkeypatch):
        svc, queue = _make_service(monkeypatch)
        msg = _Msg(json.dumps({"severity": "high"}).encode("utf-8"))

        async def go():
            await svc._handle_message("sec.t", msg)
            return queue.qsize()

        assert _run(go()) == 0
        assert svc.stats["missing_id_errors"] == 1

    def test_duplicate_skipped_after_first(self, monkeypatch):
        svc, queue = _make_service(monkeypatch)
        payload = {"finding_id": "dup-1"}
        msg = _Msg(json.dumps(payload).encode("utf-8"))

        async def go():
            await svc._handle_message("sec.t", msg)
            await svc._handle_message("sec.t", msg)
            return queue.qsize()

        assert _run(go()) == 1  # only the first landed on the queue
        assert svc.stats["messages_enqueued"] == 1
        assert svc.stats["duplicates_skipped"] == 1
        assert svc.stats["messages_consumed"] == 2

    def test_stats_surface_topics_and_group(self, monkeypatch):
        svc, _queue = _make_service(monkeypatch, topics=["t1", "t2"])
        assert svc.stats["topics"] == ["t1", "t2"]
        assert svc.stats["consumer_group"] == "test"


class TestKafkaBuildConsumer:
    def test_build_consumer_raises_without_topics(self, monkeypatch):
        cfg = KafkaConfig(enabled=True, topics=[])
        queue: asyncio.Queue = asyncio.Queue()
        svc = KafkaConsumerService(cfg, queue, RedisDedupSet("x"))

        # Stub aiokafka so the test doesn't require it installed.
        import sys
        import types

        fake_mod = types.ModuleType("aiokafka")

        class FakeConsumer:
            def __init__(self, *a, **kw):
                pass

        fake_mod.AIOKafkaConsumer = FakeConsumer
        monkeypatch.setitem(sys.modules, "aiokafka", fake_mod)

        async def go():
            with pytest.raises(ValueError, match="no topics"):
                await svc._build_consumer()

        _run(go())
