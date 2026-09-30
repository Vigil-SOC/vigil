"""Kafka commits only what reached the hand-off, partition by partition."""

from __future__ import annotations

import asyncio
import json
from typing import NamedTuple

import pytest

from core.ingestion.kafka_config import KafkaConfig
from core.ingestion.kafka_consumer_service import KafkaConsumerService

pytestmark = pytest.mark.unit


class _TP(NamedTuple):
    topic: str
    partition: int


P0, P1, P2 = _TP("t", 0), _TP("t", 1), _TP("t", 2)


class _Msg:
    def __init__(self, offset: int):
        self.offset = offset
        self.value = json.dumps({"finding_id": f"f-{offset}"}).encode()


class _Consumer:
    def __init__(self, fail_commit_on=None):
        self.commits: list = []
        self.seeks: list = []
        self._fail_commit_on = fail_commit_on

    async def commit(self, offsets=None):
        assert offsets is not None, "a bare commit() commits every partition"
        if self._fail_commit_on in offsets:
            raise RuntimeError("commit failed")
        self.commits.append(offsets)

    def seek(self, tp, offset):
        self.seeks.append((tp, offset))


class _Dedup:
    async def is_processed(self, key):
        return False

    async def mark_processed(self, key):
        pass


def _service(queue, consumer):
    cfg = KafkaConfig(enabled=True, bootstrap_servers="x:9092", topics=["t"])
    svc = KafkaConsumerService(cfg, queue, _Dedup())  # type: ignore[arg-type]
    svc._consumer = consumer
    return svc


def _batch(**parts):
    return {tp: [_Msg(o) for o in offsets] for tp, offsets in parts.values()}


@pytest.mark.asyncio
async def test_shutdown_mid_partition_commits_only_the_partitions_handed_off():
    queue: asyncio.Queue = asyncio.Queue(maxsize=3)
    consumer = _Consumer()
    svc = _service(queue, consumer)
    batches = _batch(a=(P0, [100, 101]), b=(P1, [500, 501, 502]))

    drain = asyncio.create_task(svc._drain(batches))
    await asyncio.sleep(0.05)
    assert not drain.done()  # waiting on 501 with the queue full

    svc._shutdown.set()
    assert await asyncio.wait_for(drain, timeout=1) is False
    # P1 stays at its last commit: 501 and 502 are re-read next start.
    assert consumer.commits == [{P0: 102}]
    assert queue.qsize() == 3


@pytest.mark.asyncio
async def test_an_error_rewinds_every_partition_not_yet_handed_off(monkeypatch):
    consumer = _Consumer()
    svc = _service(asyncio.Queue(), consumer)
    handle = svc._handle_message

    async def failing(topic, msg):
        if msg.offset == 501:
            raise RuntimeError("redis down")
        return await handle(topic, msg)

    monkeypatch.setattr(svc, "_handle_message", failing)
    batches = _batch(a=(P0, [100, 101]), b=(P1, [500, 501, 502]), c=(P2, [900]))

    with pytest.raises(RuntimeError):
        await svc._drain(batches)

    assert consumer.commits == [{P0: 102}]
    assert consumer.seeks == [(P1, 501), (P2, 900)]


@pytest.mark.asyncio
async def test_a_failed_commit_rewinds_the_partitions_after_it():
    consumer = _Consumer(fail_commit_on=P0)
    svc = _service(asyncio.Queue(), consumer)
    batches = _batch(a=(P0, [100, 101]), b=(P1, [500]))

    with pytest.raises(RuntimeError):
        await svc._drain(batches)

    # P0 was handed off, so it resumes after 101; P1 was never read.
    assert consumer.seeks == [(P0, 102), (P1, 500)]
