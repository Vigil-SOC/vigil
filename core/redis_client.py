"""Shared lazily-built async Redis client.

Redis is optional: when the driver is missing the caller degrades instead of
failing, so the factory returns ``None`` and names the feature that switched
off in the warning.

The client is cached process-wide, and a ``redis.asyncio`` client binds to the
event loop it is first used on. Both callers live in the API process on one
uvicorn loop, so that holds; a second event loop in the same process would get
"attached to a different loop" and needs a loop-keyed cache instead.
"""

import logging

from core.config import DEFAULT_REDIS_URL, get_settings

logger = logging.getLogger(__name__)


_client = None


def get_async_redis(disabled_feature: str):
    """Return the shared ``redis.asyncio`` client, or None if unavailable."""
    global _client
    if _client is not None:
        return _client
    try:
        from redis import asyncio as redis_asyncio
    except Exception as exc:
        logger.warning(
            "redis.asyncio unavailable: %s — %s disabled", exc, disabled_feature
        )
        return None
    url = get_settings().redis_url or DEFAULT_REDIS_URL
    _client = redis_asyncio.from_url(url, decode_responses=True)
    return _client
