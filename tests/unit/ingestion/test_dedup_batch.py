"""Batched dedup lookups: same answers as the per-item calls, fewer round-trips.

``are_processed`` / ``mark_many`` must agree with ``is_processed`` /
``mark_processed`` on the Redis path and the in-memory fallback, and the
federation runner and Kafka consumer must still skip an id repeated inside
one batch. A stub Redis counts round-trips (each awaited command or
pipeline ``execute``); fakeredis is not a dev dependency.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from core.federation.registry import FetchResult
from core.federation.runner import FederationRunner
from core.ingestion.dedup import RedisDedupSet
from core.ingestion.kafka_config import KafkaConfig
from core.ingestion.kafka_consumer_service import KafkaConsumerService


class _StubRedis:
    """Sorted-set subset of redis.asyncio, with a round-trip counter."""

    def __init__(self):
        self.zsets: dict[str, dict[str, float]] = {}
        self.round_trips = 0

    async def ping(self):
        return True

    async def zscore(self, key, member):
        self.round_trips += 1
        return self.zsets.get(key, {}).get(member)

    async def zrem(self, key, member):
        self.round_trips += 1
        self.zsets.get(key, {}).pop(member, None)

    def pipeline(self):
        return _StubPipeline(self)

    async def close(self):
        pass


class _StubPipeline:
    def __init__(self, redis: _StubRedis):
        self._redis = redis
        self._ops: list = []

    def zscore(self, key, member):
        self._ops.append(("zscore", key, member))

    def zadd(self, key, mapping):
        self._ops.append(("zadd", key, dict(mapping)))

    def zremrangebyscore(self, key, lo, hi):
        self._ops.append(("zremrangebyscore", key, lo, hi))

    def zremrangebyrank(self, key, start, stop):
        self._ops.append(("zremrangebyrank", key, start, stop))

    async def execute(self):
        self._redis.round_trips += 1
        out = []
        for op, key, *args in self._ops:
            zset = self._redis.zsets.setdefault(key, {})
            if op == "zscore":
                out.append(zset.get(args[0]))
            elif op == "zadd":
                zset.update(args[0])
                out.append(len(args[0]))
            elif op == "zremrangebyscore":
                lo, hi = args
                gone = [m for m, s in zset.items() if lo <= s <= hi]
            else:
                ranked = sorted(zset, key=lambda m: (zset[m], m))
                start, stop = args
                stop = stop + len(ranked) if stop < 0 else stop
                gone = ranked[start : stop + 1] if stop >= start else []
            if op in ("zremrangebyscore", "zremrangebyrank"):
                for m in gone:
                    del zset[m]
                out.append(len(gone))
        self._ops = []
        return out


@pytest.fixture
def stub_redis(monkeypatch):
    import redis.asyncio as aioredis

    stubs: list[_StubRedis] = []

    def from_url(url, decode_responses=True):
        stubs.append(_StubRedis())
        return stubs[-1]

    monkeypatch.setattr(aioredis, "from_url", from_url)
    return stubs


@pytest.fixture
def no_redis(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")


IDS = ["a", "b", "c", "a", "", "d", "e"]


def _make(**kw) -> RedisDedupSet:
    return RedisDedupSet("unit-batch", max_size=3, ttl_seconds=3600, **kw)


async def _per_item(dedup: RedisDedupSet, ids):
    for i in ids:
        await dedup.mark_processed(i)


@pytest.mark.parametrize("backend", ["redis", "fallback"])
def test_batch_matches_per_item(backend, request):
    request.getfixturevalue("stub_redis" if backend == "redis" else "no_redis")

    async def go():
        one, many = _make(), _make()
        await _per_item(one, ["a", "b"])
        await many.mark_many(["a", "b"])
        probe = ["a", "b", "x", "a", ""]
        expect = {i for i in probe if i and await one.is_processed(i)}
        got = await many.are_processed(probe)

        # Over max_size: TTL eviction and size trim end in the same state.
        await _per_item(one, IDS)
        await many.mark_many(IDS)
        after_one = {i for i in IDS if i and await one.is_processed(i)}
        after_many = await many.are_processed(IDS)
        return expect, got, after_one, after_many

    expect, got, after_one, after_many = asyncio.run(go())
    assert got == expect == {"a", "b"}
    assert after_many == after_one
    assert len(after_many) <= 3


def test_mark_many_keeps_ttl_and_trim(stub_redis):
    async def go():
        dedup = _make()
        await dedup.are_processed(["warm-up"])
        zset = stub_redis[0].zsets.setdefault(dedup.key, {})
        zset["stale"] = 1.0  # older than ttl_seconds
        await dedup.mark_many(["a", "b", "c", "d"])
        return dict(zset)

    zset = asyncio.run(go())
    # Stale entry evicted, oldest new id trimmed, newest three kept in order.
    assert sorted(zset, key=zset.get) == ["b", "c", "d"]


def test_one_round_trip_per_batch(stub_redis):
    ids = [f"id-{n}" for n in range(50)]

    async def go():
        dedup = RedisDedupSet("unit-batch-rt")
        await dedup.are_processed(["warm-up"])
        redis = stub_redis[0]
        redis.round_trips = 0
        await dedup.are_processed(ids)
        checked = redis.round_trips
        await dedup.mark_many(ids)
        marked = redis.round_trips - checked
        return checked, marked, await dedup.are_processed(ids)

    checked, marked, seen = asyncio.run(go())
    assert (checked, marked) == (1, 1)
    assert seen == set(ids)


def test_pending_forget_matches_is_processed(monkeypatch):
    redis = _StubRedis()
    fail = {"zrem": True}
    real_zrem = redis.zrem

    async def flaky_zrem(key, member):
        if fail["zrem"]:
            fail["zrem"] = False
            raise ConnectionError("redis down")
        await real_zrem(key, member)

    redis.zrem = flaky_zrem  # type: ignore[method-assign]
    import redis.asyncio as aioredis

    monkeypatch.setattr(aioredis, "from_url", lambda url, decode_responses=True: redis)

    async def go():
        dedup = RedisDedupSet("unit-batch-forget")
        await dedup.mark_many(["stuck", "kept"])
        await dedup.forget("stuck")  # zrem fails: pending forget
        hidden = await dedup.are_processed(["stuck", "kept"])
        await dedup.mark_many(["stuck"])
        return hidden, await dedup.are_processed(["stuck", "kept"])

    hidden, remarked = asyncio.run(go())
    assert hidden == {"kept"}
    assert remarked == {"stuck", "kept"}


def test_redis_error_falls_back(stub_redis, monkeypatch):
    import redis.asyncio as aioredis

    async def go():
        dedup = RedisDedupSet("unit-batch-err")
        await dedup.are_processed(["warm-up"])

        def broken_pipeline():
            raise ConnectionError("gone")

        def no_reconnect(url, decode_responses=True):
            raise ConnectionError("still gone")

        stub_redis[0].pipeline = broken_pipeline  # type: ignore[method-assign]
        monkeypatch.setattr(aioredis, "from_url", no_reconnect)
        await dedup.mark_many(["a", "b"])
        return await dedup.are_processed(["a", "b", "c"])

    assert asyncio.run(go()) == {"a", "b"}


# ---------------------------------------------------------------------------
# Callers: a repeated id inside one batch is enqueued once
# ---------------------------------------------------------------------------


class _Adapter:
    name = "fake"

    def __init__(self, findings):
        self.findings = findings

    def is_configured(self) -> bool:
        return True

    async def fetch(self, *, since, cursor, max_items):
        return FetchResult(findings=list(self.findings), cursor={})


def test_federation_tick_batches_and_skips_in_batch_repeat(stub_redis, monkeypatch):
    monkeypatch.setattr(
        "core.federation.runner.store.record_success", lambda *a, **k: None
    )
    findings = [
        {"external_id": "ext-1", "severity": "high"},
        {"external_id": "ext-2", "severity": "high"},
        {"external_id": "ext-1", "severity": "high"},
        {"severity": "high"},
        {"external_id": "ext-0", "severity": "high"},
    ]

    async def go():
        queue: asyncio.Queue = asyncio.Queue()
        runner = FederationRunner(output_queue=queue)
        adapter = _Adapter(findings)
        dedup = RedisDedupSet("federation:fake")
        await dedup.mark_many(["ext-0"])
        redis = stub_redis[0]
        redis.round_trips = 0
        runner._adapters[adapter.name] = adapter
        runner._dedup[adapter.name] = dedup
        await runner._do_one_tick(adapter, {"max_items": 10})
        keys = [queue.get_nowait()["dedup_key"] for _ in range(queue.qsize())]
        return keys, redis.round_trips, await dedup.are_processed(["ext-1", "ext-2"])

    keys, round_trips, marked = asyncio.run(go())
    assert keys == ["ext-1", "ext-2"]
    assert round_trips == 2  # one check, one mark
    assert marked == {"ext-1", "ext-2"}


@pytest.mark.parametrize("batched", [True, False])
def test_kafka_batch_matches_per_message(no_redis, batched):
    class _Msg:
        def __init__(self, payload):
            self.value = payload

    payloads = [
        json.dumps({"finding_id": "k-1"}),
        "not json",
        json.dumps({"finding_id": "k-2"}),
        json.dumps({"finding_id": "k-1"}),
        json.dumps({"no": "id"}),
        json.dumps({"finding_id": "k-3"}),
    ]

    async def go():
        queue: asyncio.Queue = asyncio.Queue()
        dedup = RedisDedupSet("test-kafka-batch", max_size=32)
        await dedup.mark_many(["k-3"])
        cfg = KafkaConfig(
            enabled=True,
            bootstrap_servers="localhost:9092",
            consumer_group="test",
            topics=["t"],
        )
        svc = KafkaConsumerService(cfg, queue, dedup)
        msgs = [_Msg(p) for p in payloads]
        if batched:
            await svc._handle_batch("t", msgs)
        else:
            for m in msgs:
                await svc._handle_message("t", m)
        keys = [queue.get_nowait()["dedup_key"] for _ in range(queue.qsize())]
        return keys, svc.stats, await dedup.are_processed(["k-1", "k-2"])

    keys, stats, marked = asyncio.run(go())
    assert keys == ["k-1", "k-2"]
    assert stats["messages_consumed"] == 6
    assert stats["messages_enqueued"] == 2
    assert stats["duplicates_skipped"] == 2
    assert stats["decode_errors"] == 1
    assert stats["missing_id_errors"] == 1
    assert marked == {"k-1", "k-2"}


def test_kafka_batch_is_one_round_trip_each_way(stub_redis):
    async def go():
        queue: asyncio.Queue = asyncio.Queue()
        dedup = RedisDedupSet("test-kafka-rt")
        await dedup.are_processed(["warm-up"])
        redis = stub_redis[0]
        redis.round_trips = 0
        cfg = KafkaConfig(
            enabled=True,
            bootstrap_servers="localhost:9092",
            consumer_group="test",
            topics=["t"],
        )
        svc = KafkaConsumerService(cfg, queue, dedup)

        class _Msg:
            def __init__(self, n):
                self.value = json.dumps({"finding_id": f"k-{n}"})

        await svc._handle_batch("t", [_Msg(n) for n in range(25)])
        return queue.qsize(), redis.round_trips

    assert asyncio.run(go()) == (25, 2)
