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

Verify-path failures (Redis unreachable during `is_token_revoked`) default to
**fail-closed** (reject the request). Set `REVOCATION_FAIL_OPEN=true` only if
you deliberately prefer availability over security during Redis outages.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from core.config import get_settings
from core.redis_client import get_async_redis

logger = logging.getLogger(__name__)


_JTI_PREFIX = "blacklist:jti:"
_USER_CUTOFF_PREFIX = "user_revoked_before:"

# If True, Redis failures during verification allow the request through.
# Default: False (fail-closed). Set REVOCATION_FAIL_OPEN=true for fail-open.
_FAIL_OPEN = get_settings().revocation_fail_open


# Lookup-failure outage state (process-wide). Logging is throttled to one ERROR
# per window so an outage does not bury the log in per-request lines.
_LOG_INTERVAL_SECONDS = 60
_outage_failures = 0  # failures since the last ERROR (or since the outage began)
_outage_total = 0
_outage_last_logged = 0.0


def _note_lookup_failed(exc: Exception) -> None:
    global _outage_failures, _outage_total, _outage_last_logged
    first = _outage_total == 0
    _outage_failures += 1
    _outage_total += 1
    now = time.monotonic()
    if not first and now - _outage_last_logged < _LOG_INTERVAL_SECONDS:
        return
    logger.error(
        "is_token_revoked: redis lookup failed (%s); %d failure(s) since last log;"
        " fail_%s — %s",
        exc,
        _outage_failures,
        "open" if _FAIL_OPEN else "closed",
        (
            "revocation checks are skipped"
            if _FAIL_OPEN
            else "all authenticated requests are being rejected"
        ),
    )
    _outage_failures = 0
    _outage_last_logged = now


def _note_lookup_ok() -> None:
    global _outage_failures, _outage_total
    if _outage_total == 0:
        return
    logger.warning(
        "is_token_revoked: redis lookups recovered after %d failure(s)", _outage_total
    )
    _outage_failures = _outage_total = 0


def _get_client():
    """The shared redis.asyncio client, or None if redis isn't installed."""
    return get_async_redis("token revocation")


def _now_ts() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())


async def blacklist_jti(jti: str, expires_at: Optional[datetime]) -> None:
    """
    Mark a specific token as revoked. Called on /logout.

    Raises on Redis failure so the /logout handler can surface the error.
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
        return

    await client.set(f"{_JTI_PREFIX}{jti}", "1", ex=ttl_seconds)


async def revoke_all_for_user(user_id: str) -> None:
    """
    Invalidate every outstanding token for a user by moving the cutoff
    timestamp forward.
    """
    client = _get_client()
    if client is None:
        logger.warning("revoke_all_for_user: redis client unavailable; skipping")
        return
    await client.set(f"{_USER_CUTOFF_PREFIX}{user_id}", str(_now_ts()))


async def is_token_revoked(payload: dict) -> bool:
    """
    Check whether a decoded JWT payload has been revoked.

    Behaviour on Redis failure is controlled by REVOCATION_FAIL_OPEN env var:
      - False (default): treat Redis failure as revoked → user must re-auth.
      - True: allow through on Redis failure (availability over security).
    """
    client = _get_client()
    if client is None:
        if _FAIL_OPEN:
            return False
        logger.error(
            "is_token_revoked: redis unavailable and REVOCATION_FAIL_OPEN=false"
            " — rejecting token"
        )
        return True

    try:
        revoked = await _lookup_revoked(client, payload)
    except Exception as exc:
        _note_lookup_failed(exc)
        return not _FAIL_OPEN
    _note_lookup_ok()
    return revoked


async def _lookup_revoked(client, payload: dict) -> bool:
    """Raises on Redis errors; the caller decides fail-open/closed."""
    jti = payload.get("jti")
    user_id = payload.get("user_id")

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
                    "Malformed user cutoff for %s: %r",
                    user_id,
                    cutoff_raw,
                )
                return not _FAIL_OPEN
            iat = payload.get("iat")
            if iat is None:
                return True
            if int(iat) < cutoff:
                return True

    return False
