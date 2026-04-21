"""
Password reset tokens.

Design:
- Tokens are HMAC-signed with itsdangerous.URLSafeTimedSerializer. The
  secret is reused from JWT_SECRET_KEY so we don't have yet another key
  to rotate; it's fine because the salt (purpose tag) keeps the two
  token spaces separate.
- Tokens are **single-use**: on successful confirm we write the token's
  hash into Redis, and any subsequent confirm with the same token is
  rejected. TTL on that record matches token lifetime so it auto-expires.
- If Redis is unavailable, we fall back to time-based expiry only. A
  cache outage downgrades single-use to "valid once per TTL window" —
  acceptable; the alternative is to reject all reset attempts during
  the outage, which could lock users out.

The token payload is just the user_id. We look up the user at confirm
time; if the email changed, that's fine — the reset still binds to the
original user. If the account was deactivated in the meantime, the
confirm handler rejects.
"""

import hashlib
import logging
import os
from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

logger = logging.getLogger(__name__)


RESET_TOKEN_PURPOSE = "vigil-password-reset"
DEFAULT_TTL_SECONDS = 3600  # 1 hour

_USED_TOKEN_PREFIX = "password_reset_used:"


def _ttl_seconds() -> int:
    try:
        return int(os.getenv("PASSWORD_RESET_TTL_SECONDS", str(DEFAULT_TTL_SECONDS)))
    except ValueError:
        return DEFAULT_TTL_SECONDS


def _get_serializer() -> URLSafeTimedSerializer:
    # Import lazily so tests can stub JWT_SECRET_KEY via env before auth_service loads.
    from backend.services.auth_service import JWT_SECRET_KEY
    return URLSafeTimedSerializer(JWT_SECRET_KEY, salt=RESET_TOKEN_PURPOSE)


def _token_hash(token: str) -> str:
    """Stable hash of a token used as the "used" flag key in Redis. We
    don't store the raw token because writing tokens into the cache, even
    short-lived, expands the blast radius if Redis itself is breached."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _redis_client():
    # Reuse the helper pattern from token_blacklist; keep imports local
    # so a missing redis dep doesn't break module import.
    try:
        from redis import asyncio as redis_asyncio
    except Exception as exc:
        logger.warning("redis.asyncio unavailable: %s — reset single-use check disabled", exc)
        return None
    url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    return redis_asyncio.from_url(url, decode_responses=True)


def generate_reset_token(user_id: str) -> str:
    return _get_serializer().dumps({"user_id": user_id})


async def verify_reset_token(token: str) -> Optional[str]:
    """
    Return the user_id the token was issued for, or None if the token is
    invalid, expired, or already used. Marks the token used on success so
    replay within the TTL window is rejected.
    """
    try:
        payload = _get_serializer().loads(token, max_age=_ttl_seconds())
    except SignatureExpired:
        logger.info("Password reset token expired")
        return None
    except BadSignature:
        logger.info("Password reset token has a bad signature")
        return None

    user_id = payload.get("user_id") if isinstance(payload, dict) else None
    if not user_id:
        return None

    client = _redis_client()
    if client is not None:
        try:
            key = f"{_USED_TOKEN_PREFIX}{_token_hash(token)}"
            # SET NX: succeeds only if the key didn't exist. If it did,
            # the token was already consumed.
            set_ok = await client.set(key, "1", ex=_ttl_seconds(), nx=True)
            if not set_ok:
                logger.info("Password reset token replay rejected")
                return None
        except Exception as exc:
            # Fail-open: if Redis is down we lose single-use enforcement
            # but still honour the signed TTL. A compromised reset email
            # during an outage could be replayed within the TTL — a
            # user-facing retry is the mitigation.
            logger.warning(
                "Password reset single-use check failed (%s); allowing by signature only",
                exc,
            )

    return user_id
