"""Keys older than 12 months rotate on their own, or say why not (#4181).

A background task on each replica, beside the key ring's adoption. A purpose's
current key older than ``ROTATE_AFTER`` gets a normal rotation, with its own
overlap, so planned rotation signs nobody out and stops no run; the age is
checked under the ring's lock, so replicas never rotate twice. A failed rotation
keeps the old key and puts a live condition on the bell of each holder of
``sessions.manage`` until a later one succeeds.

The wrapping secret (JWT_SECRET_KEY) rotates the same way only when it comes from
``secrets.enc``. One from the environment or ``.env`` is the operator's: the trail
notes it 30 days before 12 months, and the bell warns once it is overdue.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Optional

from sqlalchemy import text

from core.auth import key_ring, trail_events
from core.auth.mfa_coverage import SETTINGS_LINK
from core.platform import notices
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

ROTATE_AFTER = timedelta(days=365)
WARN_BEFORE = timedelta(days=30)
SWEEP_SECONDS = 3600
# Startup does not wait on the schema; the first sweep comes after adoption.
FIRST_SWEEP_SECONDS = 60
WRAPPING = "wrapping"

# What this replica knows is wrong: purpose (or WRAPPING) -> the bell's sentence.
_problems: dict[str, str] = {}


def _record(action: str, **fields) -> None:
    try:
        with unit_of_work() as session:
            key_ring._trail(session, action, **fields)
    except Exception:  # noqa: BLE001 - the sweep does not depend on the trail
        logger.exception("could not record %s", action)


def _recorded_since(action: str, since) -> bool:
    with unit_of_work() as session:
        if not trail_events.ready(session):
            return True  # nowhere to record it: do not ask again each sweep
        return (
            session.execute(
                text(
                    "SELECT 1 FROM audit_trail WHERE payload->>'action' = :a "
                    "AND occurred_at >= :s LIMIT 1"
                ),
                {"a": action, "s": since},
            ).first()
            is not None
        )


def _failed(name: str, message: str, exc: Exception, **fields) -> None:
    """Keep only the error's class: its text can carry SQL parameters (wrapped keys)."""
    if name not in _problems:
        _record("auth.key_rotation_failed", reason=type(exc).__name__, **fields)
    _problems[name] = f"{message} See the API log."


def _overdue_purposes() -> list[str]:
    with unit_of_work() as session:
        return list(
            session.execute(
                text(
                    "SELECT purpose FROM public.auth_keys WHERE state = 'current' "
                    "AND created_at < now() - make_interval(secs => :s)"
                ),
                {"s": ROTATE_AFTER.total_seconds()},
            )
            .scalars()
            .all()
        )


def _rotate_overdue() -> None:
    # Only a key that is overdue: a ring not adopted yet is not a failure.
    for purpose in _overdue_purposes():
        if purpose not in key_ring.PURPOSES:
            continue
        label = f"The {purpose} key is more than 12 months old and could not be rotated"
        try:
            version = key_ring.rotate(purpose, older_than=ROTATE_AFTER)
        except Exception as exc:  # noqa: BLE001 - keep the old key, say so, try again
            logger.exception("scheduled rotation of the %s key failed", purpose)
            _failed(purpose, f"{label}.", exc, purpose=purpose)
            continue
        if purpose in _problems:
            del _problems[purpose]
            if version is not None:
                _record("auth.key_rotation_recovered", purpose=purpose, version=version)
        if version is not None:
            logger.info("rotated the %s key, which was 12 months old", purpose)


def _check_wrapping() -> None:
    since = key_ring.wrapped_since()
    if since is None:
        return
    age = key_ring._now() - since
    if age < ROTATE_AFTER:
        _problems.pop(WRAPPING, None)  # an operator's `rewrap` ends it
    if age < ROTATE_AFTER - WARN_BEFORE:
        return
    if key_ring.wrapping_secret_is_stored():
        if age < ROTATE_AFTER:
            return
        try:
            key_ring.rotate_wrapping_secret(older_than=ROTATE_AFTER)
        except Exception as exc:  # noqa: BLE001
            logger.exception("scheduled rotation of the wrapping secret failed")
            _failed(
                WRAPPING,
                "The secret that wraps the key ring is more than 12 months old "
                "and could not be rotated.",
                exc,
            )
        else:
            if _problems.pop(WRAPPING, None):
                _record("auth.key_rotation_recovered", purpose=WRAPPING)
        return
    # From configuration: Vigil cannot change it. Say so once per age.
    if age >= ROTATE_AFTER:
        _problems[WRAPPING] = (
            "JWT_SECRET_KEY, which wraps the key ring, is more than 12 months old "
            "and is set outside Vigil. Change it where it is defined, then run "
            "`python -m core.auth.key_ring rewrap`."
        )
        action = "auth.wrapping_key_overdue"
    else:
        action = "auth.wrapping_key_due"
    if not _recorded_since(action, since):
        _record(action)


def sweep() -> None:
    _rotate_overdue()
    _check_wrapping()


async def run() -> None:
    await asyncio.sleep(FIRST_SWEEP_SECONDS)
    while True:
        try:
            await asyncio.to_thread(sweep)
        except Exception:  # noqa: BLE001 - the next sweep tries again
            logger.exception("key age sweep failed")
        await asyncio.sleep(SWEEP_SECONDS)


def overdue(user) -> list[dict[str, str]]:
    """The bell's condition, for a holder of ``sessions.manage``."""
    from core.auth.permissions import missing_permission

    if not _problems:
        return []
    if missing_permission(user.user_id, "sessions.manage") is not None:
        return []
    return [
        {"message": message, "link": SETTINGS_LINK}
        for message in list(_problems.values())
    ]


def register() -> None:
    notices.CONDITIONS.append(overdue)
