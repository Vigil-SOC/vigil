"""The one place Vigil's Python code gets a Redis URL or a shared Redis client.

``effective_redis_url`` is ``REDIS_URL`` with ``REDIS_PASSWORD``, when set,
URL-encoded into it in place of any password the URL carries. Every caller
connects through it, so a password containing ``@``, ``/``, ``:`` or ``#``
works everywhere. The result carries the password: log it only through
``core.platform.log_redaction.redact_url``.

Redis is optional: when the driver is missing the caller degrades instead of
failing, so ``get_async_redis`` returns ``None`` and names the feature that
switched off in the warning.

The client is cached process-wide, and a ``redis.asyncio`` client binds to the
event loop it is first used on. Both callers live in the API process on one
uvicorn loop, so that holds; a second event loop in the same process would get
"attached to a different loop" and needs a loop-keyed cache instead.
"""

import logging
from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit

from core.config import DEFAULT_REDIS_URL, get_settings

logger = logging.getLogger(__name__)


def _with_password(url: str, password: str) -> str:
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    # Helm substitutes the raw password into REDIS_URL (redis://:$(REDIS_PASSWORD)@…),
    # where a / or # cuts the netloc short, so take it out before parsing.
    literal = f":{password}@"
    at = rest.find(literal)
    if at != -1 and not any(c in rest[:at] for c in "/@?#"):
        rest = rest[:at] + "@" + rest[at + len(literal) :]
    parts = urlsplit(f"{scheme}://{rest}")
    userinfo, _, hostport = parts.netloc.rpartition("@")
    username = userinfo.partition(":")[0]
    netloc = f"{username}:{quote(password, safe='')}@{hostport}"
    return urlunsplit(parts._replace(netloc=netloc))


def effective_redis_url(
    url: Optional[str] = None, default: Optional[str] = DEFAULT_REDIS_URL
) -> Optional[str]:
    """``url`` (else ``REDIS_URL``, else ``default``) with ``REDIS_PASSWORD`` applied.

    Pass ``default=None`` to learn that no Redis is configured: a password
    alone configures none.
    """
    settings = get_settings()
    url = url or settings.redis_url or default
    if not url:
        return None
    secret = settings.redis_password
    password = secret.get_secret_value() if secret is not None else ""
    return _with_password(url, password) if password else url


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
    _client = redis_asyncio.from_url(effective_redis_url(), decode_responses=True)
    return _client
