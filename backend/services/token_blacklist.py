"""
Redis-backed token revocation.

Two revocation strategies, used together:

1. **Per-JTI blacklist** — `blacklist:jti:{jti}` keys. Set on logout so that
   specific token (and only that token) is rejected going forward. Key TTL
   matches the token's remaining lifetime so entries self-expire.

2. **Per-user cutoff** — `user_revoked_before:{user_id}` stores a unix
   timestamp. Any token whose `iat` claim is earlier than the cutoff is
   rejected. Set on password change / role change / "log out everywhere" —
   one write invalidates every token the user holds, without having to
   enumerate them.

Verify-path failures (Redis unreachable during `is_token_revoked`) are
**fail-open** with a WARNING: the system should not take authentication
offline because the cache is down. Write-path failures (blacklist on logout)
are logged and re-raised so the caller can decide whether to hard-fail.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


DEFAULT_REDIS_URL = "redis://localhost:6379/0"

_JTI_PREFIX = "blacklist:jti:"
_USER_CUTOFF_PREFIX = "user_revoked_before:"


_client = None


def _get_client():
    """Lazily build a redis.asyncio client. Returns None if redis isn't installed."""
    global _client
    if _client is not None:
        return _client
    try:
        from redis import asyncio as redis_asyncio  # type: ignore
    except Exception as exc:
        logger.warning("redis.asyncio unavailable: %s — token revocation disabled", exc)
        return None
    url = os.getenv("REDIS_URL", DEFAULT_REDIS_URL)
    _client = redis_asyncio.from_url(url, decode_responses=True)
    return _client


def _now_ts() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())


async def blacklist_jti(jti: str, expires_at: Optional[datetime]) -> None:
    """
    Mark a specific token as revoked. Called on /logout.

    The key expires when the token would have expired anyway, so an
    attacker who replays the token after its exp is irrelevant (JWT
    signature check already rejects it). Raises on Redis failure so the
    /logout handler can surface the error — we prefer a noisy logout
    failure over a silent revocation miss.
    """
    client = _get_client()
    if client is None:
        logger.warning("blacklist_jti: redis client unavailable; skipping")
        return

    ttl_seconds = 0
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        ttl_seconds = int((expires_at - datetime.now(tz=timezone.utc)).total_seconds())
    if ttl_seconds <= 0:
        # Token has already expired — no point blacklisting, JWT layer
        # will reject it on signature/exp.
        return

    await client.set(f"{_JTI_PREFIX}{jti}", "1", ex=ttl_seconds)


async def revoke_all_for_user(user_id: str) -> None:
    """
    Invalidate every outstanding token for a user by moving the cutoff
    timestamp forward. Called on password change, role change, or an
    explicit "log out everywhere". Any token whose `iat` is earlier than
    the stored value fails `is_token_revoked`.
    """
    client = _get_client()
    if client is None:
        logger.warning("revoke_all_for_user: redis client unavailable; skipping")
        return
    await client.set(f"{_USER_CUTOFF_PREFIX}{user_id}", str(_now_ts()))


async def is_token_revoked(payload: dict) -> bool:
    """
    Check whether a decoded JWT payload has been revoked.

    Fail-open on Redis errors — the verify path must not take auth down
    because the cache is unavailable. The tradeoff: a revocation might not
    take effect for as long as Redis is down. That is acceptable; the
    alternative is a cache outage logging everyone out.
    """
    client = _get_client()
    if client is None:
        return False

    jti = payload.get("jti")
    user_id = payload.get("user_id")

    try:
        if jti:
            exists = await client.exists(f"{_JTI_PREFIX}{jti}")
            if exists:
                return True

        if user_id:
            cutoff_raw = await client.get(f"{_USER_CUTOFF_PREFIX}{user_id}")
            if cutoff_raw is not None:
                try:
                    cutoff = int(cutoff_raw)
                except (TypeError, ValueError):
                    logger.warning(
                        "Malformed user cutoff for %s: %r", user_id, cutoff_raw
                    )
                    return False
                iat = payload.get("iat")
                if iat is None:
                    # Token predates the iat addition — treat as revoked so
                    # old tokens force a re-login after a cutoff is set.
                    return True
                if int(iat) < cutoff:
                    return True
    except Exception as exc:
        logger.warning(
            "is_token_revoked: redis lookup failed (%s); failing open", exc
        )
        return False

    return False
