"""
Shared slowapi Limiter for auth and other sensitive endpoints.

Uses Redis as the distributed storage backend so rate limits apply across
workers. Falls back to in-memory storage if Redis is unreachable so a cache
outage does not take authentication offline.
"""

import logging
import os

from slowapi import Limiter
from slowapi.util import get_remote_address

logger = logging.getLogger(__name__)


_redis_url = os.getenv("REDIS_URL")

try:
    limiter = Limiter(
        key_func=get_remote_address,
        storage_uri=_redis_url,
        strategy="fixed-window",
    )
except Exception as exc:
    logger.warning(
        "Rate limiter Redis storage unavailable (%s); falling back to in-memory. "
        "Limits will not be shared across processes.",
        exc,
    )
    limiter = Limiter(key_func=get_remote_address, strategy="fixed-window")
