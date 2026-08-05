# Shared slowapi Limiter. Redis keeps limits consistent across workers; an
# unreachable Redis degrades to in-memory rather than taking auth offline.

import logging

from slowapi import Limiter
from slowapi.util import get_remote_address

from core.config import get_settings

logger = logging.getLogger(__name__)


def _in_memory() -> Limiter:
    return Limiter(key_func=get_remote_address, strategy="fixed-window")


# slowapi connects lazily, so construction succeeding proves nothing: probe the
# storage here or the first rate-limited request 500s instead of degrading.
def _build_limiter() -> Limiter:
    url = get_settings().redis_url
    if not url:
        return _in_memory()
    try:
        candidate = Limiter(
            key_func=get_remote_address, storage_uri=url, strategy="fixed-window"
        )
        if not candidate.limiter.storage.check():
            raise RuntimeError("storage health check failed")
        return candidate
    except Exception as exc:
        logger.warning(
            "Rate limiter Redis storage unavailable (%s); falling back to in-memory. "
            "Limits will not be shared across processes.",
            exc,
        )
        return _in_memory()


limiter = _build_limiter()
