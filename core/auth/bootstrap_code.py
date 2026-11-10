"""First-account setup code.

Read from the key ring's setup key, so every API replica computes the same
value and nothing is stored. Anyone who can read the ring's keys can already
forge sessions, which is the same trust. A key from before a rotation still
matches for its overlap, so a code logged by an older replica works.
"""

from __future__ import annotations

import asyncio
import hmac
import logging

from sqlalchemy.orm import Session

from core.auth import key_ring
from core.storage.models import User

logger = logging.getLogger(__name__)

# The formatted log line starts with SETUP_CODE_LOG_MARK; scripts grep that and
# do not recompute it.
SETUP_CODE_LOG_TEMPLATE = "First-run setup code: %s"
SETUP_CODE_LOG_MARK = "First-run setup code:"

# 128 bits. The bootstrap route is 5/minute per IP, and the value is copied
# from a log, not chosen by the operator.
_SETUP_CODE_HEX_CHARS = 32


def _code(key: key_ring.Key) -> str:
    return key.secret.hex()[:_SETUP_CODE_HEX_CHARS]


def setup_code() -> str:
    return _code(key_ring.current("setup"))


def setup_code_matches(submitted: str | None) -> bool:
    """Constant-time check. Missing and wrong are the same result."""
    if submitted is None:
        return False
    candidate = submitted.strip()
    if not candidate:
        return False
    # No early exit: every live key is compared, so timing says nothing.
    matched = False
    for key in key_ring.live("setup"):
        expected = _code(key)
        matched |= len(candidate) == len(expected) and hmac.compare_digest(
            candidate, expected
        )
    return matched


def has_human_user(session: Session) -> bool:
    """Whether bootstrap is closed. A service account (Medic's gateway) cannot
    sign anyone in, so it leaves bootstrap open."""
    return (
        session.query(User.user_id).filter(User.service_account.is_(False)).first()
        is not None
    )


def _announce_once() -> None:
    """One attempt. Returns once the code is logged or a human user exists;
    raises when the query fails."""
    from core.storage.unit_of_work import unit_of_work

    with unit_of_work() as session:
        if has_human_user(session):
            return
    # Template stays free of the value so log grouping does not record it twice.
    logger.warning(SETUP_CODE_LOG_TEMPLATE, setup_code())


async def announce_setup_code() -> None:
    """Log the code once while no human user exists.

    Run as a background task: on Helm the schema arrives after the pods start,
    so a failed query is retried (capped backoff) until it succeeds. The code is
    never logged while the query fails. POST still requires it either way.
    """
    delay = 2.0
    warned = False
    while True:
        try:
            # The session is synchronous; keep it off the event loop.
            await asyncio.to_thread(_announce_once)
            return
        except Exception as exc:
            if not warned:
                warned = True
                logger.warning(
                    "Skipping first-run setup code announcement (%s); retrying "
                    "until the database is ready. Bootstrap still requires it",
                    exc,
                )
            else:
                logger.debug("Setup code announcement retry failed: %s", exc)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30.0)
