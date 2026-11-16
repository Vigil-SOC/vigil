"""Keys older than the rotation period rotate on their own, or say why not (#4181).

A background task on each replica, beside the key ring's adoption. A purpose's
current key older than the period gets a normal rotation, with its own overlap,
so planned rotation signs nobody out and stops no run; the age is checked under
the ring's lock, so replicas never rotate twice. A failed rotation keeps the old
key and puts a live condition on the bell of each holder of ``sessions.manage``
until a later one succeeds.

The period is ``KEY_ROTATION_DAYS``: a whole number of days, 12 months (365)
when unset, and never longer. A longer value, or one Vigil cannot read, is not
used. 12 months still applies, and the API logs that once at start.

The wrapping secret (JWT_SECRET_KEY) rotates the same way only when it comes from
``secrets.enc``. One from the environment or ``.env`` is the operator's: the trail
notes it 30 days before the period ends, and the bell warns once it is overdue.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta

from dotenv import dotenv_values
from sqlalchemy import text

from core.auth import key_ring, trail_events
from core.auth.mfa_coverage import SETTINGS_LINK
from core.config import _settings_env_file
from core.platform import notices
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

PERIOD_NAME = "KEY_ROTATION_DAYS"
# 12 months is the longest the epic allows. A shorter period is the operator's.
CEILING_DAYS = 365
WARN_BEFORE = timedelta(days=30)
_DEFAULT_PERIOD = timedelta(days=CEILING_DAYS)
# Logged at start, not on each sweep.
_period_noted = False
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


def _whole_days(text: str) -> int | None:
    body = text[1:] if text.startswith("+") else text
    if not body.isascii() or not body.isdigit():
        return None
    return int(body)


def _configured_raw() -> tuple[str | None, bool]:
    """The host's value, and whether ``.env`` could not be read.

    Blank is unset. A value in the environment wins over ``.env``, including a
    blank one: that is how Helm and Compose leave the 12-month default in place.
    """
    if PERIOD_NAME in os.environ:  # noqa: ENV001 - host setting, not a Settings field
        return os.environ[PERIOD_NAME], False  # noqa: ENV001
    try:
        env_file = _settings_env_file()
        if env_file is None:
            return None, False
        values = dotenv_values(env_file) or {}
    except Exception:  # noqa: BLE001 - a bad .env must not stop rotation
        return None, True
    for key, value in values.items():
        if key is not None and key.upper() == PERIOD_NAME:
            return None if value is None else str(value), False
    return None, False


def rotation_period() -> tuple[timedelta, str | None]:
    """The period to enforce, and one warning when the configured value is not used."""
    raw, unreadable = _configured_raw()
    if unreadable:
        return _DEFAULT_PERIOD, (
            f"{PERIOD_NAME} could not be read; keys rotate every 12 months."
        )
    if raw is None or str(raw).strip() == "":
        return _DEFAULT_PERIOD, None
    text = str(raw).strip()
    days = _whole_days(text)
    if days is None or days < 1 or days > CEILING_DAYS:
        return _DEFAULT_PERIOD, (
            f"{PERIOD_NAME}={text} is not used; keys rotate every 12 months."
        )
    return timedelta(days=days), None


def note_configured_period() -> None:
    """Log once, at start, when the configured period is not used."""
    global _period_noted
    if _period_noted:
        return
    _period_noted = True
    warning = rotation_period()[1]
    if warning:
        logger.warning("%s", warning)


def _age_words(period: timedelta) -> str:
    days = period.days
    unit = "day" if days == 1 else "days"
    return f"more than {days} {unit} old"


def _due_after(period: timedelta) -> timedelta:
    """When an outside wrapping secret is first noted. Never negative, so a
    period of 30 days or less is noted on the next sweep."""
    early = period - WARN_BEFORE
    if early < timedelta(0):
        return timedelta(0)
    return early


def _overdue_purposes(period: timedelta) -> list[str]:
    with unit_of_work() as session:
        return list(
            session.execute(
                text(
                    "SELECT purpose FROM public.auth_keys WHERE state = 'current' "
                    "AND created_at < now() - make_interval(secs => :s)"
                ),
                {"s": period.total_seconds()},
            )
            .scalars()
            .all()
        )


def _rotate_overdue(period: timedelta) -> None:
    # Only a key that is overdue: a ring not adopted yet is not a failure.
    words = _age_words(period)
    for purpose in _overdue_purposes(period):
        if purpose not in key_ring.PURPOSES:
            continue
        label = f"The {purpose} key is {words} and could not be rotated"
        try:
            version = key_ring.rotate(purpose, older_than=period)
        except Exception as exc:  # noqa: BLE001 - keep the old key, say so, try again
            logger.exception("scheduled rotation of the %s key failed", purpose)
            _failed(purpose, f"{label}.", exc, purpose=purpose)
            continue
        if purpose in _problems:
            del _problems[purpose]
            if version is not None:
                _record("auth.key_rotation_recovered", purpose=purpose, version=version)
        if version is not None:
            logger.info("rotated the %s key, which was %s", purpose, words)


def _check_wrapping(period: timedelta) -> None:
    since = key_ring.wrapped_since()
    if since is None:
        return
    age = key_ring._now() - since
    words = _age_words(period)
    if age < period:
        _problems.pop(WRAPPING, None)  # an operator's `rewrap` ends it
    if age < _due_after(period):
        return
    if key_ring.wrapping_secret_is_stored():
        if age < period:
            return
        try:
            key_ring.rotate_wrapping_secret(older_than=period)
        except Exception as exc:  # noqa: BLE001
            logger.exception("scheduled rotation of the wrapping secret failed")
            _failed(
                WRAPPING,
                f"The secret that wraps the key ring is {words} "
                "and could not be rotated.",
                exc,
            )
        else:
            if _problems.pop(WRAPPING, None):
                _record("auth.key_rotation_recovered", purpose=WRAPPING)
        return
    # From configuration: Vigil cannot change it. Say so once per age.
    if age >= period:
        _problems[WRAPPING] = (
            f"JWT_SECRET_KEY, which wraps the key ring, is {words} "
            "and is set outside Vigil. Change it where it is defined, then run "
            "`python -m core.auth.key_ring rewrap`."
        )
        action = "auth.wrapping_key_overdue"
    else:
        action = "auth.wrapping_key_due"
    if not _recorded_since(action, since):
        _record(action)


def sweep() -> None:
    period, _warning = rotation_period()
    _rotate_overdue(period)
    _check_wrapping(period)


async def run() -> None:
    note_configured_period()
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
    note_configured_period()
    notices.CONDITIONS.append(overdue)
