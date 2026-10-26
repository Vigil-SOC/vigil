# Shared slowapi Limiter. Redis keeps limits consistent across workers; an
# unreachable Redis degrades to in-memory rather than taking auth offline.

import logging

from slowapi import Limiter

from core.redis_client import build_redis_limiter, redact_logged_text
from services.api.middleware.client_address import client_host

logger = logging.getLogger(__name__)


def _in_memory() -> Limiter:
    return Limiter(key_func=client_host, strategy="fixed-window")


# slowapi connects lazily, so construction succeeding proves nothing: probe the
# storage here or the first rate-limited request 500s instead of degrading.
def _build_limiter() -> Limiter:
    try:
        # A Redis that rejects writes (noeviction + full) or drops mid-run must
        # not turn every rate-limited route, login included, into a 500.
        # None means REDIS_URL is unset — a password alone does not select Redis.
        candidate = build_redis_limiter(
            Limiter,
            key_func=client_host,
            strategy="fixed-window",
            in_memory_fallback_enabled=True,
        )
        if candidate is None:
            return _in_memory()
        if not candidate.limiter.storage.check():
            raise RuntimeError("storage health check failed")
        return candidate
    except Exception as exc:
        logger.error(
            "Rate limiter Redis storage unavailable (%s); falling back to in-memory."
            " The limiter stays in-memory until the process restarts (no re-probe),"
            " and limits are not shared across workers.",
            redact_logged_text(str(exc)),
        )
        return _in_memory()


limiter = _build_limiter()
