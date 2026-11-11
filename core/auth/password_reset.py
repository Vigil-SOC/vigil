"""
Password reset tokens.

Design:
- Tokens are HMAC-signed with itsdangerous.URLSafeTimedSerializer under the
  key ring's reset key. It signs with the newest key and verifies under every
  live one, so a rotation leaves outstanding links valid for their hour.
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

from core.auth import key_ring

logger = logging.getLogger(__name__)


RESET_TOKEN_PURPOSE = "vigil-password-reset"


class ResetClaim(NamedTuple):
    user_id: str
    # isoformat of password_changed_at when issued; None if it was NULL.
    changed_at: Optional[str]
    # Issued by sign-in because the password must change, or by an
    # administrator's script: the minimum age does not apply to it.
    forced: bool = False


# A reset link's lifetime. Fixed: PASSWORD_RESET_TTL_SECONDS is retired.
RESET_TTL_SECONDS = 3600


def _get_serializer() -> URLSafeTimedSerializer:
    # Oldest first: itsdangerous signs with the last key and verifies with all.
    keys = [k.secret for k in key_ring.live("reset")]
    return URLSafeTimedSerializer(keys, salt=RESET_TOKEN_PURPOSE)


def _stamp(changed_at: Optional[datetime]) -> Optional[str]:
    # isoformat keeps microseconds, so two sets in the same second still differ.
    return changed_at.isoformat() if changed_at else None


def generate_reset_token(user, forced: bool = False) -> str:
    """Sign a reset token for ``user`` (anything with user_id and
    password_changed_at), valid until its password next changes. ``forced``
    marks a link the minimum password age does not apply to."""
    claims = {"user_id": user.user_id, "changed_at": _stamp(user.password_changed_at)}
    if forced:
        claims["forced"] = True
    return _get_serializer().dumps(claims)


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
        payload = _get_serializer().loads(token, max_age=RESET_TTL_SECONDS)
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
    return ResetClaim(
        payload["user_id"], payload["changed_at"], payload.get("forced") is True
    )


def is_current(claim: ResetClaim, user) -> bool:
    """True while the user's password hasn't changed since the token was issued."""
    return claim.user_id == user.user_id and claim.changed_at == _stamp(
        user.password_changed_at
    )
