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
from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from core.config import get_settings
from core.redis_client import get_async_redis

logger = logging.getLogger(__name__)


RESET_TOKEN_PURPOSE = "vigil-password-reset"

_USED_TOKEN_PREFIX = "password_reset_used:"


def _ttl_seconds() -> int:
    return get_settings().password_reset_ttl_seconds


def _get_serializer() -> URLSafeTimedSerializer:
    # Import lazily so tests can stub JWT_SECRET_KEY via env before auth_service loads.
    from core.auth.auth_service import JWT_SECRET_KEY

    return URLSafeTimedSerializer(JWT_SECRET_KEY, salt=RESET_TOKEN_PURPOSE)


def _token_hash(token: str) -> str:
    """Stable hash of a token used as the "used" flag key in Redis. We
    don't store the raw token because writing tokens into the cache, even
    short-lived, expands the blast radius if Redis itself is breached."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _redis_client():
    return get_async_redis("reset single-use check")


def generate_reset_token(user_id: str) -> str:
    return _get_serializer().dumps({"user_id": user_id})


def reset_link(base_url: str, token: str) -> str:
    """Console URL that opens the reset screen for ``token``."""
    return f"{base_url.rstrip('/')}/reset-password?token={token}"


def decode_reset_token(token: str) -> Optional[str]:
    """
    Return the user_id the token was issued for, or None if its signature
    is bad or its TTL has passed. Does not check or record single use.
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
    return user_id or None


async def consume_reset_token(token: str) -> bool:
    """
    Mark the token used. Return False if it was already used, so a replay
    within the TTL window is rejected.
    """
    client = _redis_client()
    if client is None:
        return True
    try:
        key = f"{_USED_TOKEN_PREFIX}{_token_hash(token)}"
        if not await client.set(key, "1", ex=_ttl_seconds(), nx=True):
            logger.info("Password reset token replay rejected")
            return False
    except Exception as exc:
        logger.warning(
            "Password reset single-use check failed (%s); allowing by signature only",
            exc,
        )
    return True
