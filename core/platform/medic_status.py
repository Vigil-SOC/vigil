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
# 270 s, not 300, leaves a poll's slack: at 300 the live Down landed at 298-301 s
# (V2-2).
POLL_INTERVAL_S = 60
POLL_TIMEOUT_S = 10
DOWN_AFTER_S = 270
_POLL = timedelta(seconds=POLL_INTERVAL_S)
# How far before the first failure an outage may have begun: one interval, plus
# a margin for a replica's skipped tick. A conservative bound; it only matters
# when there's no recent success to count from.
_NOTICE_LAG = timedelta(seconds=POLL_INTERVAL_S + POLL_TIMEOUT_S)
_RUNNING_FOR = 2 * _POLL
# Nobody has written the row for this long (Medic was off, or no poller ran):
# what it says is history, not Medic's state now.
_STALE_AFTER = 3 * _POLL
_DOWN_AFTER = timedelta(seconds=DOWN_AFTER_S)


class MedicStatus(str, Enum):
    OFF = "off"  # the flag is false: Medic exits at start, nothing is polled
    RUNNING = "running"  # answered the status op within the last 2 polls
    DOWN = "down"  # failing, and unseen for DOWN_AFTER_S (C5 §5.4)
    UNKNOWN = "unknown"  # never seen, between running and down, stale, or unreadable


@dataclass(frozen=True)
class LastSeen:
    """The fields of the ``medic_last_seen`` row the status depends on."""

    last_seen_at: Optional[datetime]
    first_failed_at: Optional[datetime]
    failure_kind: Optional[str]
    updated_at: Optional[datetime] = None


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
    if seen.updated_at is not None and now - seen.updated_at >= _STALE_AFTER:
        return MedicStatus.UNKNOWN
    if seen.first_failed_at is not None:
        # The outage started at the last success. Without one, the first
        # failure counts from _NOTICE_LAG before it: a Medic last seen long ago (the backend was off) that
        # refuses once on restart gets the same grace as any other outage.
        since = seen.first_failed_at - _NOTICE_LAG
        if seen.last_seen_at is not None:
            since = max(since, seen.last_seen_at)
        if now - since >= _DOWN_AFTER:
            return MedicStatus.DOWN
    if seen.last_seen_at is not None and now - seen.last_seen_at < _RUNNING_FOR:
        return MedicStatus.RUNNING
    return MedicStatus.UNKNOWN
