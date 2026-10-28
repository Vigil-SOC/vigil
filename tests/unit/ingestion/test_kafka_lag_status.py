"""Per-partition committed offset, high watermark and lag on Kafka stats (#2095)."""

from __future__ import annotations

import asyncio
import json
from collections import namedtuple

from core.ingestion.dedup import RedisDedupSet
from core.ingestion.kafka_config import KafkaConfig
from core.ingestion.kafka_consumer_service import KafkaConsumerService
from tests.unit._acking_queue import AckingQueue

TP = namedtuple("TP", "topic partition")
T0 = TP("security.findings", 0)


class _Msg:
    def __init__(self, offset, finding_id):
        self.offset = offset
        self.value = json.dumps({"finding_id": finding_id}).encode()


class _FakeConsumer:
    """Records calls; ``committed`` is the only remote offset call it serves."""

    def __init__(self, committed=None, highwater=None, assigned=(T0,)):
        self.assigned = set(assigned)
        self.broker_committed = dict(committed or {})
        self.high = dict(highwater or {})
        self.calls: list[str] = []

    def assignment(self):
        return set(self.assigned)

    def highwater(self, tp):
        return self.high.get(tp)

    async def committed(self, tp):
        self.calls.append("committed")
        return self.broker_committed.get(tp)

    async def commit(self, offsets):
        self.calls.append("commit")
        self.broker_committed.update(offsets)

    def seek(self, tp, offset):
        self.calls.append("seek")

    async def end_offsets(self, tps):  # remote; must never be used
        self.calls.append("end_offsets")

    async def position(self, tp):  # may be remote; must never be used
        self.calls.append("position")


def _make_service(monkeypatch, consumer):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    cfg = KafkaConfig(
        enabled=True,
        bootstrap_servers="localhost:9092",
        consumer_group="test",
        topics=["security.findings"],
    )
    svc = KafkaConsumerService(cfg, AckingQueue(), RedisDedupSet("lag", max_size=32))
    svc._consumer = consumer
    return svc


def _row(svc):
    (row,) = svc.stats["partitions"]
    return row


def test_lag_is_high_watermark_minus_committed(monkeypatch):
    consumer = _FakeConsumer(committed={T0: 10}, highwater={T0: 25})
    svc = _make_service(monkeypatch, consumer)
    asyncio.run(svc._refresh_partitions())
    assert _row(svc) == {
        "topic": "security.findings",
        "partition": 0,
        "committed": 10,
        "high_watermark": 25,
        "lag": 15,
    }


def test_rewind_keeps_committed_and_lag_grows(monkeypatch):
    consumer = _FakeConsumer(committed={T0: 10}, highwater={T0: 25})
    svc = _make_service(monkeypatch, consumer)

    async def go():
        await svc._refresh_partitions()

        async def not_stored(topic, msgs):
            return False

        svc._handle_batch = not_stored
        assert await svc._process_partition(T0, [_Msg(10, "a"), _Msg(11, "b")]) is False
        consumer.high[T0] = 30  # more messages arrive
        await svc._refresh_partitions()

    asyncio.run(go())
    assert _row(svc)["committed"] == 10
    assert _row(svc)["lag"] == 20
    assert "seek" in consumer.calls


def test_batch_commit_updates_committed_without_remote_lookup(monkeypatch):
    consumer = _FakeConsumer(committed={T0: 10}, highwater={T0: 12})
    svc = _make_service(monkeypatch, consumer)

    async def go():
        await svc._refresh_partitions()
        consumer.calls.clear()
        assert await svc._process_partition(T0, [_Msg(10, "a"), _Msg(11, "b")])
        await svc._refresh_partitions()

    asyncio.run(go())
    assert consumer.calls == ["commit"]
    assert _row(svc)["committed"] == 12
    assert _row(svc)["lag"] == 0


def test_unknown_offsets_report_null_lag(monkeypatch):
    t1 = TP("security.findings", 1)
    consumer = _FakeConsumer(highwater={t1: 5}, assigned=(t1, T0))
    svc = _make_service(monkeypatch, consumer)
    asyncio.run(svc._refresh_partitions())
    rows = svc.stats["partitions"]
    assert [r["partition"] for r in rows] == [0, 1]
    assert rows[0]["high_watermark"] is None and rows[0]["lag"] is None
    assert rows[1]["committed"] is None and rows[1]["lag"] is None


def test_revoked_partition_is_dropped(monkeypatch):
    t1 = TP("security.findings", 1)
    consumer = _FakeConsumer(
        committed={T0: 1, t1: 1}, highwater={T0: 2, t1: 2}, assigned=(T0, t1)
    )
    svc = _make_service(monkeypatch, consumer)

    async def go():
        await svc._refresh_partitions()
        consumer.assigned = {T0}
        await svc._refresh_partitions()

    asyncio.run(go())
    assert _row(svc)["partition"] == 0


def test_idle_and_disconnected_report_empty(monkeypatch):
    consumer = _FakeConsumer(committed={T0: 10}, highwater={T0: 25})
    svc = _make_service(monkeypatch, consumer)
    assert svc.stats["partitions"] == []  # idle, never connected

    async def build():
        return consumer

    async def start():
        pass

    async def stop():
        pass

    async def getmany(timeout_ms):
        shutdown.set()
        return {}

    consumer.start, consumer.stop, consumer.getmany = start, stop, getmany
    svc._build_consumer = build
    shutdown = asyncio.Event()

    async def go():
        await svc.run(shutdown)

    asyncio.run(go())
    assert svc.stats["connected"] is False
    assert svc.stats["partitions"] == []
