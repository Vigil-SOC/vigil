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
``PollState``). A warning is logged on fallback.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_REDIS_URL = "redis://localhost:6379/0"
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
        self.redis_url = redis_url or os.getenv("REDIS_URL", DEFAULT_REDIS_URL)
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds

        self._redis = None
        self._fallback: set[str] = set()
        self._fallback_warned = False
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
        except Exception as e:
            self._warn_fallback(f"init failed: {e}")
            self._redis = None
        return self._redis

    def _warn_fallback(self, reason: str):
        if not self._fallback_warned:
            logger.warning(
                "RedisDedupSet[%s] using in-memory fallback (%s)",
                self.namespace,
                reason,
            )
            self._fallback_warned = True

    async def is_processed(self, finding_id: str) -> bool:
        if not finding_id:
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
        now = time.time()
        r = await self._get_redis()
        if r is None:
            self._fallback.add(finding_id)
            if len(self._fallback) > self.max_size:
                # FIFO-ish trim
                for item in list(self._fallback)[: self.max_size // 2]:
                    self._fallback.discard(item)
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
