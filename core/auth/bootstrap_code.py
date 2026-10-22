"""First-account setup code.

Derived from ``JWT_SECRET_KEY`` with its own purpose tag, the same way
password-reset tokens are, so every API replica computes the same value and
nothing is stored. Anyone who can read the signing key can already forge
sessions, which is the same trust. The tag is not ``vigil-password-reset``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from sqlalchemy.orm import Session

from core.storage.models import User

logger = logging.getLogger(__name__)

# Distinct from password_reset.RESET_TOKEN_PURPOSE. The formatted log line
# starts with SETUP_CODE_LOG_MARK; scripts grep that and do not recompute it.
SETUP_CODE_PURPOSE = "vigil-bootstrap-setup"
SETUP_CODE_LOG_TEMPLATE = "First-run setup code: %s"
SETUP_CODE_LOG_MARK = "First-run setup code:"

# 128 bits. The bootstrap route is 5/minute per IP, and the value is copied
# from a log, not chosen by the operator.
_SETUP_CODE_HEX_CHARS = 32


def setup_code() -> str:
    # Imported at call time so tests can still patch JWT_SECRET_KEY, matching
    # password_reset._get_serializer.
    from core.auth.auth_service import JWT_SECRET_KEY

    digest = hmac.new(
        JWT_SECRET_KEY.encode("utf-8"),
        SETUP_CODE_PURPOSE.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:_SETUP_CODE_HEX_CHARS]


def setup_code_matches(submitted: str | None) -> bool:
    """Constant-time check. Missing and wrong are the same result."""
    if submitted is None:
        return False
    candidate = submitted.strip()
    if not candidate:
        return False
    expected = setup_code()
    if len(candidate) != len(expected):
        return False
    return hmac.compare_digest(candidate, expected)


def has_human_user(session: Session) -> bool:
    """Whether bootstrap is closed. A service account (Medic's gateway) cannot
    sign anyone in, so it leaves bootstrap open."""
    return (
        session.query(User.user_id).filter(User.service_account.is_(False)).first()
        is not None
    )


def announce_setup_code() -> None:
    """Log the code once while no human user exists.

    A failed query must not stop startup and must not print a code. POST still
    requires the code either way.
    """
    try:
        from core.storage.unit_of_work import unit_of_work

        with unit_of_work() as session:
            if has_human_user(session):
                return
        code = setup_code()
    except Exception as exc:
        logger.warning(
            "Skipping first-run setup code announcement (%s); bootstrap still requires it",
            exc,
        )
        return
    # Template stays free of the value so log grouping does not record it twice.
    logger.warning(SETUP_CODE_LOG_TEMPLATE, code)
