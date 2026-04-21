"""Unit tests for the Redis-backed dedup helper.

These tests exercise the in-memory fallback path (i.e. Redis is
unreachable) because that's what runs deterministically in CI without
a live Redis. A corresponding integration test that hits real Redis
belongs under tests/integration and is tagged with the ``integration``
marker.
"""

from __future__ import annotations

import asyncio
import pytest

from daemon.dedup import RedisDedupSet


@pytest.fixture
def no_redis(monkeypatch):
    """Force the dedup helper into fallback mode by pointing it at a dead URL."""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    return monkeypatch


def _make() -> RedisDedupSet:
    # Small max_size so size-cap trimming is easy to exercise
    return RedisDedupSet("unit-test", max_size=4, ttl_seconds=3600)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestRedisDedupFallback:
    def test_is_processed_empty(self, no_redis):
        dedup = _make()

        async def go():
            return await dedup.is_processed("abc")

        assert _run(go()) is False

    def test_mark_and_check(self, no_redis):
        dedup = _make()

        async def go():
            await dedup.mark_processed("finding-1")
            return await dedup.is_processed("finding-1")

        assert _run(go()) is True

    def test_empty_finding_id_is_noop(self, no_redis):
        dedup = _make()

        async def go():
            # empty strings / None should not blow up and should not be marked
            await dedup.mark_processed("")
            return await dedup.is_processed("")

        assert _run(go()) is False

    def test_fallback_trims_when_over_max(self, no_redis):
        dedup = _make()  # max_size=4

        async def go():
            for i in range(10):
                await dedup.mark_processed(f"id-{i}")
            # fallback set should be bounded (trims roughly to max_size/2)
            return len(dedup._fallback)

        size = _run(go())
        assert size <= 4


class TestRedisDedupWithFakeRedis:
    """Substitute a stub Redis client to exercise the happy path without a server."""

    def test_marks_survive_new_instance(self, monkeypatch):
        # Shared in-process store shared between two RedisDedupSet instances
        store: dict[str, dict[str, float]] = {}

        class StubPipeline:
            def __init__(self, store):
                self._store = store
                self._ops = []

            def zadd(self, key, mapping):
                self._ops.append(("zadd", key, mapping))
                return self

            def zremrangebyscore(self, key, min_score, max_score):
                self._ops.append(("zremrangebyscore", key, min_score, max_score))
                return self

            def zremrangebyrank(self, key, start, stop):
                self._ops.append(("zremrangebyrank", key, start, stop))
                return self

            async def execute(self):
                for op in self._ops:
                    if op[0] == "zadd":
                        _, key, mapping = op
                        self._store.setdefault(key, {}).update(mapping)
                    elif op[0] == "zremrangebyscore":
                        _, key, lo, hi = op
                        bucket = self._store.setdefault(key, {})
                        for k, v in list(bucket.items()):
                            if lo <= v <= hi:
                                bucket.pop(k, None)
                    elif op[0] == "zremrangebyrank":
                        # Best-effort emulation; size-cap trimming is exercised
                        # separately via the in-memory fallback test.
                        pass
                self._ops = []

        class StubRedis:
            def __init__(self, store):
                self._store = store

            async def ping(self):
                return True

            async def zscore(self, key, member):
                bucket = self._store.get(key) or {}
                return bucket.get(member)

            async def zcard(self, key):
                return len(self._store.get(key) or {})

            async def delete(self, key):
                self._store.pop(key, None)

            def pipeline(self):
                return StubPipeline(self._store)

            async def close(self):
                pass

        def fake_from_url(url, decode_responses=True):
            return StubRedis(store)

        fake_aioredis = type(
            "FakeAioredisModule", (), {"from_url": staticmethod(fake_from_url)}
        )
        import sys

        monkeypatch.setitem(sys.modules, "redis.asyncio", fake_aioredis)

        async def go():
            d1 = RedisDedupSet("unit-shared")
            await d1.mark_processed("keep-1")
            await d1.mark_processed("keep-2")
            await d1.close()

            d2 = RedisDedupSet("unit-shared")
            seen_keep_1 = await d2.is_processed("keep-1")
            seen_missing = await d2.is_processed("never-marked")
            await d2.close()
            return seen_keep_1, seen_missing

        seen_keep_1, seen_missing = _run(go())
        assert seen_keep_1 is True
        assert seen_missing is False
