"""Redis-backed durable deduplication for ingestion pipelines.

Replaces the in-memory ``PollState.processed_ids`` set (bounded, FIFO,
lost on restart) with a Redis sorted set that survives daemon restarts
and is shared across processes.

Used by both the polling ingester (``daemon/poller.py``) and the Kafka
consumer (``services/kafka_consumer_service.py``). Each caller picks a
namespace (e.g. ``"splunk"``, ``"kafka"``) so dedup sets are isolated.

If Redis is unavailable at init time or any call fails, the helper falls
back to an in-memory set so ingestion keeps working — the trade-off is
that restarts may re-process findings (same behaviour as the old
``PollState``). An error is logged when an instance moves from Redis to the
fallback, and a warning when it reconnects; repeated failed probes while in
fallback are silent. Ids marked during an outage stay in memory only and are
not merged back into Redis on recovery.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Iterable, Optional

from core.config import DEFAULT_REDIS_URL, get_settings

logger = logging.getLogger(__name__)

DEFAULT_MAX_SIZE = 10_000
DEFAULT_TTL_SECONDS = 86_400  # 24h


class RedisDedupSet:
    """Durable finding-ID dedup set, per-namespace.

    Backed by a Redis sorted set keyed ``vigil:dedup:{namespace}`` where
    the score is the insertion unix timestamp. On each ``mark_processed``
    call, entries older than ``ttl_seconds`` are evicted and the set is
    trimmed to ``max_size`` (oldest-first) to bound memory.
    """

    def __init__(
        self,
        namespace: str,
        *,
        redis_url: Optional[str] = None,
        max_size: int = DEFAULT_MAX_SIZE,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ):
        self.namespace = namespace
        self.key = f"vigil:dedup:{namespace}"
        self.redis_url = redis_url or get_settings().redis_url or DEFAULT_REDIS_URL
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds

        self._redis = None
        self._fallback: set[str] = set()
        # Ids we tried to forget while Redis rejected the zrem. Checked before
        # the sorted set so a failed delete cannot leave the id looking handled.
        # mark_processed clears an id here — a later successful enqueue sticks.
        self._pending_forget: set[str] = set()
        self._in_fallback = False
        self._lock = asyncio.Lock()

    async def _get_redis(self):
        if self._redis is not None:
            return self._redis
        try:
            import redis.asyncio as aioredis  # type: ignore

            self._redis = aioredis.from_url(self.redis_url, decode_responses=True)
            # Probe connection so we fail fast here rather than per-call
            await self._redis.ping()
            logger.info(
                "RedisDedupSet[%s] connected to %s", self.namespace, self.redis_url
            )
            if self._in_fallback:
                self._in_fallback = False
                logger.warning(
                    "RedisDedupSet[%s] Redis recovered; %d id(s) marked during the"
                    " outage exist only in memory and are not in Redis",
                    self.namespace,
                    len(self._fallback),
                )
        except Exception as e:
            self._warn_fallback(f"init failed: {e}")
            self._redis = None
        return self._redis

    def _warn_fallback(self, reason: str):
        """Log once per Redis -> fallback transition, not per failed call."""
        if not self._in_fallback:
            logger.error(
                "RedisDedupSet[%s] Redis unavailable, using in-memory fallback"
                " (%s); dedup state will not survive restarts or be shared",
                self.namespace,
                reason,
            )
            self._in_fallback = True

    async def is_processed(self, finding_id: str) -> bool:
        if not finding_id:
            return False
        if finding_id in self._pending_forget:
            if await self._remove_from_redis(finding_id):
                self._pending_forget.discard(finding_id)
            return False
        r = await self._get_redis()
        if r is None:
            return finding_id in self._fallback
        try:
            return await r.zscore(self.key, finding_id) is not None
        except Exception as e:
            self._warn_fallback(f"zscore error: {e}")
            self._redis = None
            return finding_id in self._fallback

    async def mark_processed(self, finding_id: str) -> None:
        if not finding_id:
            return
        self._pending_forget.discard(finding_id)
        now = time.time()
        r = await self._get_redis()
        if r is None:
            self._fallback_add(finding_id)
            return
        try:
            async with self._lock:
                pipe = r.pipeline()
                pipe.zadd(self.key, {finding_id: now})
                # TTL eviction: drop entries older than ttl_seconds
                pipe.zremrangebyscore(self.key, 0, now - self.ttl_seconds)
                # Size cap: trim oldest if over max_size
                pipe.zremrangebyrank(self.key, 0, -(self.max_size + 1))
                await pipe.execute()
        except Exception as e:
            self._warn_fallback(f"zadd error: {e}")
            self._redis = None
            self._fallback.add(finding_id)

    def _fallback_add(self, finding_id: str) -> None:
        self._fallback.add(finding_id)
        if len(self._fallback) > self.max_size:
            # FIFO-ish trim
            for item in list(self._fallback)[: self.max_size // 2]:
                self._fallback.discard(item)

    async def are_processed(self, finding_ids: Iterable[str]) -> set[str]:
        """Batched ``is_processed``: the subset of ``finding_ids`` already seen.

        One Redis round-trip (a pipeline of ``ZSCORE``, which every server
        version supports) for the whole batch instead of one per id. Gives the
        same answer per id as ``is_processed``, including pending forgets and
        the in-memory fallback.
        """
        ids = list(dict.fromkeys(i for i in finding_ids if i))
        pending = [i for i in ids if i in self._pending_forget]
        for finding_id in pending:
            if await self._remove_from_redis(finding_id):
                self._pending_forget.discard(finding_id)
        ids = [i for i in ids if i not in pending]
        if not ids:
            return set()
        r = await self._get_redis()
        if r is None:
            return {i for i in ids if i in self._fallback}
        try:
            pipe = r.pipeline()
            for finding_id in ids:
                pipe.zscore(self.key, finding_id)
            scores = await pipe.execute()
        except Exception as e:
            self._warn_fallback(f"zscore error: {e}")
            self._redis = None
            return {i for i in ids if i in self._fallback}
        return {i for i, score in zip(ids, scores) if score is not None}

    async def mark_many(self, finding_ids: Iterable[str]) -> None:
        """Batched ``mark_processed``: one Redis round-trip for the batch.

        Each id gets its own increasing timestamp, in order, as sequential
        ``mark_processed`` calls would; a repeated id takes its last one. TTL eviction and the size cap run once
        after the adds; that leaves the same members as running them after
        every add, because each add is the newest entry in the set.
        """
        ids = [i for i in finding_ids if i]
        if not ids:
            return
        self._pending_forget.difference_update(ids)
        # Strictly increasing scores keep insertion order for the size trim;
        # equal scores would rank by member name instead.
        now = time.time()
        scores = {finding_id: now + n * 1e-6 for n, finding_id in enumerate(ids)}
        r = await self._get_redis()
        if r is None:
            for finding_id in ids:
                self._fallback_add(finding_id)
            return
        try:
            async with self._lock:
                pipe = r.pipeline()
                pipe.zadd(self.key, scores)
                pipe.zremrangebyscore(self.key, 0, now - self.ttl_seconds)
                pipe.zremrangebyrank(self.key, 0, -(self.max_size + 1))
                await pipe.execute()
        except Exception as e:
            self._warn_fallback(f"zadd error: {e}")
            self._redis = None
            self._fallback.update(ids)

    async def forget(self, finding_id: str) -> None:
        """Drop one id so a later poll can enqueue it again.

        Removes it from Redis (``zrem``) and from this instance's in-memory
        fallback. Another ``RedisDedupSet`` does not share that fallback, so
        the caller must be the instance that marked the id. A failed ``zrem``
        stays pending on this instance until a later call succeeds, so
        ``is_processed`` does not read the member back out of Redis.
        """
        if not finding_id:
            return
        self._fallback.discard(finding_id)
        if await self._remove_from_redis(finding_id):
            self._pending_forget.discard(finding_id)
            return
        self._pending_forget.add(finding_id)

    async def _remove_from_redis(self, finding_id: str) -> bool:
        """True when Redis accepted the delete. False when it could not."""
        r = await self._get_redis()
        if r is None:
            return False
        try:
            await r.zrem(self.key, finding_id)
            return True
        except Exception as e:
            self._warn_fallback(f"zrem error: {e}")
            self._redis = None
            return False

    async def size(self) -> int:
        r = await self._get_redis()
        if r is None:
            return len(self._fallback)
        try:
            return int(await r.zcard(self.key))
        except Exception:
            return len(self._fallback)

    async def clear(self) -> None:
        """Drop the entire dedup set — test/admin helper."""
        self._fallback.clear()
        r = await self._get_redis()
        if r is None:
            return
        try:
            await r.delete(self.key)
        except Exception as e:
            logger.debug("RedisDedupSet[%s] clear failed: %s", self.namespace, e)

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.close()
            except Exception:
                pass
            self._redis = None
