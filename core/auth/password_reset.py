"""
Password reset tokens.

Design:
- Tokens are HMAC-signed with itsdangerous.URLSafeTimedSerializer. The
  secret is reused from JWT_SECRET_KEY so we don't have yet another key
  to rotate; it's fine because the salt (purpose tag) keeps the two
  token spaces separate.
- Tokens are **single-use** by binding them to the account's password
  state: the payload carries the user_id and the user's
  ``password_changed_at`` at issue time (null if never changed). Every
  password set — a completed reset or a change by any route — moves that
  timestamp, so the confirm handler rejects any link whose stamp no longer
  matches. A completed reset therefore voids that link and every other
  outstanding one, with no cache involved; the confirm path locks the user
  row so two confirms of one link cannot both pass.
- A token without the stamp (issued before this rule) is invalid.

We look up the user at confirm time; if the email changed, that's fine —
the reset still binds to the original user. If the account was deactivated
in the meantime, the confirm handler rejects.
"""

import logging
import os
from datetime import datetime
from typing import NamedTuple, Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from core.config import get_settings

logger = logging.getLogger(__name__)


RESET_TOKEN_PURPOSE = "vigil-password-reset"


class ResetClaim(NamedTuple):
    user_id: str
    # isoformat of password_changed_at when issued; None if it was NULL.
    changed_at: Optional[str]


def _ttl_seconds() -> int:
    return get_settings().password_reset_ttl_seconds


def _get_serializer() -> URLSafeTimedSerializer:
    # Import lazily so tests can stub JWT_SECRET_KEY via env before auth_service loads.
    from core.auth.auth_service import JWT_SECRET_KEY

    return URLSafeTimedSerializer(JWT_SECRET_KEY, salt=RESET_TOKEN_PURPOSE)


def _stamp(changed_at: Optional[datetime]) -> Optional[str]:
    # isoformat keeps microseconds, so two sets in the same second still differ.
    return changed_at.isoformat() if changed_at else None


def generate_reset_token(user) -> str:
    """Sign a reset token for ``user`` (anything with user_id and
    password_changed_at), valid until its password next changes."""
    return _get_serializer().dumps(
        {"user_id": user.user_id, "changed_at": _stamp(user.password_changed_at)}
    )


def default_base_url(settings) -> str:
    """Console URL for reset links: VIGIL_FRONTEND_URL, else this host's console.
    Never derived from the request, whose Host header the client controls."""
    if settings.vigil_frontend_url:
        return settings.vigil_frontend_url
    port = 6987 if os.path.exists("/.dockerenv") else 6988
    return f"http://localhost:{port}{settings.vigil_context_path.rstrip('/')}"


def reset_link(base_url: str, token: str) -> str:
    """Console URL that opens the reset screen for ``token``."""
    return f"{base_url.rstrip('/')}/reset-password?token={token}"


def decode_reset_token(token: str) -> Optional[ResetClaim]:
    """
    Return what the token was issued for, or None if its signature is bad,
    its TTL has passed, or it predates the password stamp. Check the claim
    against the user with ``is_current`` before acting on it.
    """
    try:
        payload = _get_serializer().loads(token, max_age=_ttl_seconds())
    except SignatureExpired:
        logger.info("Password reset token expired")
        return None
    except BadSignature:
        logger.info("Password reset token has a bad signature")
        return None

    if not isinstance(payload, dict) or not payload.get("user_id"):
        return None
    if "changed_at" not in payload:
        logger.info("Password reset token predates the password stamp")
        return None
    return ResetClaim(payload["user_id"], payload["changed_at"])


def is_current(claim: ResetClaim, user) -> bool:
    """True while the user's password hasn't changed since the token was issued."""
    return claim.user_id == user.user_id and claim.changed_at == _stamp(
        user.password_changed_at
    )
