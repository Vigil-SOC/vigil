"""Shared Redis clients, and the one effective Redis URL.

Every client and every URL a client is handed comes from here. ``REDIS_URL``
is read once. ``REDIS_PASSWORD``, when set and non-empty, replaces any
password in that URL and is percent-encoded first. Callers that need their
own connection (another event loop, a short-lived sync client) ask for a new
client rather than building one or reading ``redis_url`` themselves.

The shared async client is cached process-wide, and a ``redis.asyncio`` client
binds to the event loop it is first used on. Both callers of
``get_async_redis`` live in the API process on one uvicorn loop, so that
holds; a second event loop in the same process would get "attached to a
different loop" and needs ``open_async_redis`` instead.
"""

import logging
from typing import Optional
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from arq.connections import RedisSettings

from core.config import DEFAULT_REDIS_URL, get_settings

logger = logging.getLogger(__name__)


class _RedisSettings(RedisSettings):
    """``RedisSettings`` whose ``repr`` does not spell the password."""

    def __repr__(self) -> str:
        parts = []
        for key, value in self.__dict__.items():
            if key == "password":
                value = "***" if value else None
            parts.append(f"{key}={value!r}")
        return "RedisSettings({})".format(", ".join(parts))


_client = None


def _password_text(settings) -> str:
    raw = getattr(settings, "redis_password", None)
    if raw is None:
        return ""
    reveal = getattr(raw, "get_secret_value", None)
    if reveal is not None:
        raw = reveal()
    return raw if isinstance(raw, str) else ""


def _with_password(url: str, password: str) -> str:
    """``url`` with ``password`` installed as the userinfo password.

    Helm writes ``redis://:$(REDIS_PASSWORD)@host`` and the kubelet substitutes
    the password unencoded. A ``/`` or ``#`` then cuts ``urlsplit`` short, so
    when that raw password is sitting in the string, lift it out before parsing.
    The username is kept; the password is encoded with ``quote(..., safe="")``.
    """
    if not password:
        return url
    literal = f":{password}@"
    stripped = url.replace(literal, ":@", 1)
    parts = urlsplit(stripped)
    if not parts.hostname:
        return url
    # Keep the username bytes exactly as they appeared. urlsplit does not
    # percent-decode userinfo, and re-encoding is not a round-trip (`+` and
    # `%2f` change). ARQ does not decode the username, so a rewritten token
    # would authenticate as someone else.
    userinfo, sep, hostport = parts.netloc.rpartition("@")
    if not sep:
        hostport = parts.netloc
        username = ""
    else:
        username, _, _rest = userinfo.partition(":")
    encoded = quote(password, safe="")
    userinfo = f"{username}:{encoded}" if username else f":{encoded}"
    return urlunsplit(
        (
            parts.scheme,
            f"{userinfo}@{hostport}",
            parts.path,
            parts.query,
            parts.fragment,
        )
    )


def effective_redis_url() -> str:
    """URL clients dial. Falls back to the local default when unset."""
    settings = get_settings()
    return _with_password(
        settings.redis_url or DEFAULT_REDIS_URL, _password_text(settings)
    )


def configured_redis_url() -> Optional[str]:
    """Effective URL when ``REDIS_URL`` is set, else None.

    A ``REDIS_PASSWORD`` on its own does not select Redis. The rate limiter
    stays in memory until a URL is configured.
    """
    settings = get_settings()
    if not settings.redis_url:
        return None
    return _with_password(settings.redis_url, _password_text(settings))


def redact_logged_text(text: str) -> str:
    """Drop the configured Redis password, raw and URL-encoded, from a log line."""
    password = _password_text(get_settings())
    if not password:
        return text
    encoded = quote(password, safe="")
    if encoded != password:
        text = text.replace(encoded, "***")
    return text.replace(password, "***")


def open_async_redis(url: Optional[str] = None):
    """A new ``redis.asyncio`` client. Pass ``url`` only for an explicit override."""
    from redis import asyncio as redis_asyncio

    return redis_asyncio.from_url(url or effective_redis_url(), decode_responses=True)


def open_sync_redis(**kwargs):
    """A new sync client. The context manager closes its pool."""
    import redis

    return redis.from_url(effective_redis_url(), decode_responses=True, **kwargs)


def arq_redis_settings() -> RedisSettings:
    """ARQ connection settings from the effective URL.

    ARQ parses the DSN itself and keeps what reading host/port/db/password off
    the URL by hand drops: the scheme, so ``rediss://`` sets ssl, and the
    username. ``urlparse`` leaves the password percent-encoded and ARQ passes
    it through, so a key containing ``@`` or ``/`` would authenticate with the
    escapes still in it. redis-py unquotes in ``from_url``; this matches it.

    ``create_pool`` retries on connect errors. The defaults (1s timeout, 5
    retries) give up about 10s into a cold Redis. These allow ~45s for a
    refused connection, ~90s if every try times out.
    """
    parsed = RedisSettings.from_dsn(effective_redis_url())
    settings = _RedisSettings(**parsed.__dict__)
    if settings.password:
        settings.password = unquote(settings.password)
    settings.conn_timeout = 3
    settings.conn_retries = 15
    settings.conn_retry_delay = 3
    return settings


def build_redis_limiter(limiter_cls, **kwargs):
    """``limiter_cls`` pointed at Redis, or None when no URL is configured.

    ``storage_uri`` is set here so call sites never place a password in a URL.
    """
    url = configured_redis_url()
    if not url:
        return None
    return limiter_cls(storage_uri=url, **kwargs)


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
