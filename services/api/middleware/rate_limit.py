# Shared slowapi Limiter. Redis keeps limits consistent across workers; an
# unreachable Redis degrades to in-memory rather than taking auth offline.

import logging

from slowapi import Limiter

from core.config import get_settings
from services.api.middleware.client_address import client_host

logger = logging.getLogger(__name__)


def _in_memory() -> Limiter:
    return Limiter(key_func=client_host, strategy="fixed-window")


# slowapi connects lazily, so construction succeeding proves nothing: probe the
# storage here or the first rate-limited request 500s instead of degrading.
def _build_limiter() -> Limiter:
    url = get_settings().redis_url
    if not url:
        return _in_memory()
    try:
        # A Redis that rejects writes (noeviction + full) or drops mid-run must
        # not turn every rate-limited route, login included, into a 500.
        candidate = Limiter(
            key_func=client_host,
            storage_uri=url,
            strategy="fixed-window",
            in_memory_fallback_enabled=True,
        )
        if not candidate.limiter.storage.check():
            raise RuntimeError("storage health check failed")
        return candidate
    except Exception as exc:
        logger.error(
            "Rate limiter Redis storage unavailable (%s); falling back to in-memory."
            " The limiter stays in-memory until the process restarts (no re-probe),"
            " and limits are not shared across workers.",
            exc,
        )
        return _in_memory()


limiter = _build_limiter()
