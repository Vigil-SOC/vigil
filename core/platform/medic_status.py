"""Medic's status as the backend sees it (C5 §5.3-5.4, C8, D2-18).

``VIGIL_MEDIC_ENABLED`` is the master switch on every install shape, and the
backend reads the same setting Medic does, so a Medic that is off never shows as
down. Medic's rule (``services/medic/app/config.py``) is copied, not imported:
Medic imports nothing from ``core`` and the backend nothing from Medic, and
``tests/unit/platform/test_medic_status.py`` holds the two to the same answers.

With the flag on, the state comes from the ``medic_last_seen`` row the poller
writes (``core/platform/medic_last_seen.py``), always against the backend's own
clock. This module is pure: it never touches the network or the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from core.config import Settings, get_settings
from core.time import utcnow

# Exactly Medic's "on" spellings. Anything else, unrecognised values included,
# is off: the same fail-safe reading Medic makes.
_TRUE = frozenset({"true", "1", "yes", "on"})

# C5 §5.3: one poll a minute. PRD US-05: a dead Medic shows Down within 5 min.
POLL_INTERVAL_S = 60
DOWN_AFTER_S = 300
_POLL = timedelta(seconds=POLL_INTERVAL_S)
_RUNNING_FOR = 2 * _POLL
_DOWN_AFTER = timedelta(seconds=DOWN_AFTER_S)


class MedicStatus(str, Enum):
    OFF = "off"  # the flag is false: Medic exits at start, nothing is polled
    RUNNING = "running"  # answered the status op within the last 2 polls
    DOWN = "down"  # failing, and unseen for DOWN_AFTER_S (C5 §5.4)
    UNKNOWN = "unknown"  # never seen, between running and down, or no row to read


@dataclass(frozen=True)
class LastSeen:
    """The fields of the ``medic_last_seen`` row the status depends on."""

    last_seen_at: Optional[datetime]
    first_failed_at: Optional[datetime]
    failure_kind: Optional[str]


def medic_enabled(settings: Optional[Settings] = None) -> bool:
    value = (settings or get_settings()).vigil_medic_enabled
    return value.strip().lower() in _TRUE


def medic_status(
    settings: Optional[Settings] = None,
    seen: Optional[LastSeen] = None,
    now: Optional[datetime] = None,
) -> MedicStatus:
    if not medic_enabled(settings):
        return MedicStatus.OFF
    if seen is None:
        return MedicStatus.UNKNOWN
    now = now or utcnow()
    if seen.first_failed_at is not None:
        # The outage started at the last success. The first failure is only
        # noticed up to one poll later, so it alone counts from one poll before
        # it: a Medic last seen long ago (the backend was off) that refuses
        # once on restart gets the same grace as any other outage.
        since = seen.first_failed_at - _POLL
        if seen.last_seen_at is not None:
            since = max(since, seen.last_seen_at)
        if now - since >= _DOWN_AFTER:
            return MedicStatus.DOWN
    if seen.last_seen_at is not None and now - seen.last_seen_at < _RUNNING_FOR:
        return MedicStatus.RUNNING
    return MedicStatus.UNKNOWN
