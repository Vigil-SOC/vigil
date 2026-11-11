"""Keys that are a year old (#4181): rotated when Vigil can write them, else warned.

A background task starts beside the key ring's adoption. Each pass rotates every
ring key whose current version is 12 months old, with that purpose's normal
overlap, so a planned rotation signs nobody out and stops no run. The key that
wraps the ring (JWT_SECRET_KEY) is rotated the same way only when it lives in the
secret store; when it comes from configuration (the environment or ``.env``) the
operator changes it, so the trail notes it 30 days before it is a year old and a
condition shows once it is.

A rotation that fails keeps the old key and shows a condition to the holders of
``sessions.manage`` until a rotation succeeds; each change of state is a trail
entry. The state is per process, like the other conditions.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text

from core.auth import key_ring
from core.auth.mfa_coverage import SETTINGS_LINK
from core.platform import notices
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

MAX_AGE = timedelta(days=365)
WARN_BEFORE = timedelta(days=30)
FIRST_PASS_S = 60.0
PASS_EVERY_S = 6 * 3600.0
RETRY_AFTER_FAILURE_S = 15 * 60.0
WRAPPING = "wrapping"

# What is wrong now, by purpose or "wrapping" (a rotation failed, or the
# wrapping key is overdue and is not ours to rotate).
_problems: dict[str, str] = {}


def warning(user) -> list[dict[str, str]]:
    from core.auth.permissions import missing_permission

    if not _problems or missing_permission(user.user_id, "sessions.manage") is not None:
        return []
    return [{"message": m, "link": SETTINGS_LINK} for m in _problems.values()]


def register() -> None:
    notices.CONDITIONS.append(warning)


def _state(key: str, message: Optional[str], action: Optional[str] = None) -> None:
    """Set or clear one problem. A change of state is a trail entry: ``action``
    when set, "recovered" when cleared (None writes none)."""
    if (message is None and key not in _problems) or _problems.get(key) == message:
        return
    if message is None:
        del _problems[key]
        action = "auth.key_rotation_recovered"
    else:
        _problems[key] = message
    if action is None:
        return
    try:
        with unit_of_work() as session:
            key_ring._trail(session, action, purpose=key)
    except Exception:  # noqa: BLE001 - the condition stands without the entry
        logger.exception("key age: trail entry for %s not written", key)


def _rotate_purpose(purpose: str, now: datetime) -> bool:
    """Rotate when the current key is a year old, checked under the ring's lock
    so a second replica does not rotate it again. Whether it rotated."""
    with unit_of_work() as session:
        key_ring._advisory(session)
        created = session.execute(
            text(
                "SELECT created_at FROM public.auth_keys "
                "WHERE purpose = :p AND state = 'current'"
            ),
            {"p": purpose},
        ).scalar()
        if created is None or now - created < MAX_AGE:
            return False
        key_ring.rotate(purpose, session=session)
        return True


def _wrapping_writable() -> bool:
    """Only a secret in the store is ours to change; configuration outranks nothing
    we could write, and Helm's per-pod state directory refuses writes."""
    from core.secrets import get_secrets_manager, secret_writes_refused

    return bool(
        not secret_writes_refused()
        and get_secrets_manager().encrypted_backend.get("JWT_SECRET_KEY")
    )


def rotate_wrapping() -> None:
    """A new JWT_SECRET_KEY in the store, every ring row wrapped under it. The old
    value goes back if the database write fails, so the ring stays readable."""
    from core.secrets import get_secret, set_secret

    old = get_secret("JWT_SECRET_KEY") or ""
    new = secrets.token_urlsafe(48)
    written = False
    try:
        with unit_of_work() as session:
            key_ring._advisory(session)
            key_ring._read(
                session, old
            )  # a ring that does not open is not ours to move
            if not set_secret("JWT_SECRET_KEY", new):
                raise key_ring.KeyRingError(
                    "the new key could not be written to the secret store"
                )
            written = True
            moved = key_ring.rewrap(session, old, new)
            key_ring._trail(session, "auth.wrapping_key_rotated", keys=moved)
    except Exception:
        # The write above is not in the transaction, so put the old value back.
        if written and not set_secret("JWT_SECRET_KEY", old):
            logger.error("key age: the old JWT_SECRET_KEY could not be restored")
        raise
    key_ring.forget()


def _entry_since(session, action: str, since: datetime) -> bool:
    return (
        session.execute(
            text(
                "SELECT 1 FROM audit_trail WHERE payload->>'action' = :a "
                "AND occurred_at >= :since LIMIT 1"
            ),
            {"a": action, "since": since},
        ).first()
        is not None
    )


def check(now: Optional[datetime] = None) -> bool:
    """One pass. Returns whether any rotation failed (the next pass comes sooner)."""
    now = now or key_ring._now()
    failed = False
    for purpose in key_ring.PURPOSES:
        try:
            if _rotate_purpose(purpose, now):
                logger.info("key age: rotated the %s key", purpose)
            _state(purpose, None)
        except Exception as exc:  # noqa: BLE001 - keep the old key, say so
            failed = True
            logger.warning("key age: %s rotation failed: %s", purpose, exc)
            _state(
                purpose,
                f"The {purpose} key is a year old and could not be rotated. "
                "The old key stays in use until a rotation succeeds.",
                "auth.key_rotation_failed",
            )
    failed |= _check_wrapping(now)
    return failed


def _check_wrapping(now: datetime) -> bool:
    try:
        since = key_ring.wrapped_since()
        if since is None or now - since < MAX_AGE - WARN_BEFORE:
            _state(WRAPPING, None)
            return False
        if _wrapping_writable():
            if now - since >= MAX_AGE:
                rotate_wrapping()
                logger.info("key age: rotated the key that wraps the key ring")
            _state(WRAPPING, None)
            return False
        # Configuration holds it: say so ahead of time, then keep saying so.
        overdue = now - since >= MAX_AGE
        action = "auth.wrapping_key_overdue" if overdue else "auth.wrapping_key_due"
        with unit_of_work() as session:
            first = not _entry_since(session, action, since)
            if first:
                key_ring._trail(session, action)
        if overdue:  # the entry above is this state's trail entry
            _state(
                WRAPPING,
                "JWT_SECRET_KEY, which wraps the key ring, is over a year old. It is "
                "set outside the secret store, so Vigil cannot rotate it; see "
                "docs/key-recovery.md.",
            )
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("key age: wrapping key rotation failed: %s", exc)
        _state(
            WRAPPING,
            "The key that wraps the key ring is over a year old and could not be "
            "rotated. The old key stays in use until a rotation succeeds.",
            "auth.key_rotation_failed",
        )
        return True


async def run() -> None:
    await asyncio.sleep(FIRST_PASS_S)
    while True:
        delay = PASS_EVERY_S
        try:
            if await asyncio.to_thread(check):
                delay = RETRY_AFTER_FAILURE_S
        except Exception:  # noqa: BLE001 - the next pass tries again
            logger.exception("key age pass failed")
        await asyncio.sleep(delay)
