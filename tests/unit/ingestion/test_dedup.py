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

from core.ingestion.dedup import RedisDedupSet


@pytest.fixture
def no_redis(monkeypatch):
    """Force the dedup helper into fallback mode by pointing it at a dead URL."""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    return monkeypatch


def _make() -> RedisDedupSet:
    # Small max_size so size-cap trimming is easy to exercise
    return RedisDedupSet("unit-test", max_size=4, ttl_seconds=3600)


def _run(coro):
    return asyncio.run(coro)


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

            async def zrem(self, key, member):
                bucket = self._store.get(key) or {}
                bucket.pop(member, None)
                return 1

            async def delete(self, key):
                self._store.pop(key, None)

            def pipeline(self):
                return StubPipeline(self._store)

            async def close(self):
                pass

        def fake_from_url(url, decode_responses=True):
            return StubRedis(store)

        import redis.asyncio as _aioredis

        monkeypatch.setattr(_aioredis, "from_url", fake_from_url)

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

    def test_forget_zrem_is_visible_to_a_new_instance(self, monkeypatch):
        store: dict[str, dict[str, float]] = {}

        class StubRedis:
            def __init__(self, store):
                self._store = store

            async def ping(self):
                return True

            async def zscore(self, key, member):
                return (self._store.get(key) or {}).get(member)

            async def zrem(self, key, member):
                (self._store.get(key) or {}).pop(member, None)

            def pipeline(self):
                outer = self

                class _Pipe:
                    def zadd(self, key, mapping):
                        outer._store.setdefault(key, {}).update(mapping)
                        return self

                    def zremrangebyscore(self, *args):
                        return self

                    def zremrangebyrank(self, *args):
                        return self

                    async def execute(self):
                        return []

                return _Pipe()

            async def close(self):
                pass

        import redis.asyncio as _aioredis

        monkeypatch.setattr(
            _aioredis, "from_url", lambda url, decode_responses=True: StubRedis(store)
        )

        async def go():
            d1 = RedisDedupSet("unit-forget")
            await d1.mark_processed("gone")
            await d1.forget("gone")
            await d1.close()
            d2 = RedisDedupSet("unit-forget")
            seen = await d2.is_processed("gone")
            await d2.close()
            return seen

        assert _run(go()) is False

    def test_failed_zrem_stays_unprocessed_until_retry(self, monkeypatch):
        members: dict[str, float] = {}
        fail = {"zrem": True}

        class FlakyRedis:
            async def ping(self):
                return True

            async def zscore(self, key, member):
                return members.get(member)

            async def zrem(self, key, member):
                if fail["zrem"]:
                    fail["zrem"] = False
                    raise ConnectionError("redis down")
                members.pop(member, None)

            def pipeline(self):
                class _Pipe:
                    def zadd(self, key, mapping):
                        members.update(mapping)
                        return self

                    def zremrangebyscore(self, *args):
                        return self

                    def zremrangebyrank(self, *args):
                        return self

                    async def execute(self):
                        return []

                return _Pipe()

            async def close(self):
                pass

        import redis.asyncio as _aioredis

        monkeypatch.setattr(
            _aioredis, "from_url", lambda url, decode_responses=True: FlakyRedis()
        )

        async def go():
            dedup = RedisDedupSet("unit-zrem-fail")
            await dedup.mark_processed("stuck")
            await dedup.forget("stuck")
            # The member is still in Redis, but this instance must not treat
            # it as handled. The next check retries the delete.
            still_there = "stuck" in members
            hidden = await dedup.is_processed("stuck")
            gone = "stuck" not in members
            await dedup.mark_processed("stuck")
            remarked = await dedup.is_processed("stuck")
            await dedup.close()
            return still_there, hidden, gone, remarked

        still_there, hidden, gone, remarked = _run(go())
        assert still_there is True
        assert hidden is False
        assert gone is True
        assert remarked is True


def test_forget_clears_only_this_instance_fallback(no_redis):
    """A new RedisDedupSet does not share the in-memory fallback."""

    async def go():
        d1 = _make()
        d2 = _make()
        await d1.mark_processed("keep")
        await d2.forget("keep")
        still_marked = await d1.is_processed("keep")
        await d1.forget("keep")
        cleared = await d1.is_processed("keep")
        return still_marked, cleared

    still_marked, cleared = _run(go())
    assert still_marked is True
    assert cleared is False
