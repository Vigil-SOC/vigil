"""The supervisory model's scheduled check, and the status the page reads from it.

About once an interval the daemon's scheduler runs :func:`tick`:

1. One tick at a time, across replicas. A tick that finds another running skips,
   and so does one that finds a check made less than an interval ago.
2. Medic is read once. The sweep holds and releases suggestions on that read, and
   every proposal made in this tick is held on the same read. When Medic cannot be
   read, or the sweep fails, holds stay as they are and new proposals go in open.
3. Each registered check runs in turn. A check returns proposals: the keyword
   arguments of :func:`core.supervisory.store.propose`, without ``held_incident``,
   which the tick supplies. A check that raises, or makes a proposal the store
   refuses, is logged and named on the state row; the other checks still run.
4. Each applied change that is due is followed up: whether results got better
   after it (:mod:`core.supervisory.followup`).
5. When it ran, and when the next is due, go on the one-row state table.

The scheduler asks every few minutes (:func:`poll_seconds`); the tick decides
from the state table whether a check is due, so a restart or a slow pass of the
scheduler's other tasks does not push a check back by a whole interval.

The scheduler runs its tasks one after another, so a check must stay light: work
that takes long goes through a queued job, not the tick.

The model is off by default and switched on with
``DAEMON_SUPERVISORY_ENABLED=true``. While it is off the daemon registers no
tick, the page reads ``off``, and suggestions stay as they are.
``DAEMON_SUPERVISORY_INTERVAL`` sets the interval in seconds; it is never under
five minutes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from core.config import Settings, get_settings
from core.storage.connection import get_db_manager
from core.storage.models.supervisory import SupervisoryWorkerState
from core.storage.unit_of_work import unit_of_work
from core.supervisory import followup, medic_hold, store

logger = logging.getLogger(__name__)

Check = Callable[[], Iterable[dict[str, Any]]]

MIN_INTERVAL = 300
MAX_POLL = 300
# The scheduler wakes once a minute, so a tick due now may start a little early.
_SLACK = timedelta(seconds=60)
# Two-key advisory locks never collide with the one-key per-setting locks.
_TRY_LOCK = (
    "SELECT pg_try_advisory_lock(hashtext('supervisory'), hashtext('worker-tick'))"
)
_UNLOCK = "SELECT pg_advisory_unlock(hashtext('supervisory'), hashtext('worker-tick'))"

_CHECKS: dict[str, Check] = {}


def register(name: str, check: Check) -> None:
    """Add a check the tick runs, by name; a second registration replaces the first."""
    _CHECKS[name] = check


def registered() -> list[str]:
    return list(_CHECKS)


def enabled(settings: Settings | None = None) -> bool:
    return (settings or get_settings()).daemon_supervisory_enabled


def interval_seconds(settings: Settings | None = None) -> int:
    return max(MIN_INTERVAL, (settings or get_settings()).daemon_supervisory_interval)


def poll_seconds(interval: int) -> int:
    """How often the scheduler asks: a check then starts at most this late."""
    return min(MAX_POLL, max(MIN_INTERVAL, interval) // 2)


def tick(settings: Settings | None = None) -> dict[str, Any] | None:
    """Run one check if it is due and no other replica is running one.

    Returns what it did, or None when it is off, not due, or another tick holds
    the lock.
    """
    settings = settings or get_settings()
    if not enabled(settings):
        return None
    manager = get_db_manager()
    manager.initialize()  # a no-op once the process has its engine
    # Its own connection, outside any transaction: nothing is held open while
    # Medic is read or the checks run.
    with manager.engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as conn:
        if not conn.execute(text(_TRY_LOCK)).scalar():
            return None
        try:
            return _tick(settings, interval_seconds(settings))
        finally:
            conn.execute(text(_UNLOCK))


def _tick(settings: Settings, interval: int) -> dict[str, Any] | None:
    now = datetime.now(UTC)
    state = _read_state()
    if state is not None and now - state.last_check_at < (
        timedelta(seconds=interval) - _SLACK
    ):
        return None
    proposed, failed = 0, []
    try:
        incidents = medic_hold.open_incidents(settings)
        swept = medic_hold.sweep(settings, incidents) if incidents is not None else None
    except Exception:  # noqa: BLE001 - read as Medic unreadable; the checks still run
        logger.exception("supervisory hold sweep failed")
        incidents, swept = None, None
        failed.append("medic_hold")
    # Importing the package registers the built-in checks.
    import core.supervisory.checks  # noqa: F401

    for name, check in list(_CHECKS.items()):
        try:
            for proposal in check():
                held = medic_hold.incident_for(proposal["fn"], incidents or [])
                if store.propose(**proposal, held_incident=held) is not None:
                    proposed += 1
        except Exception:  # noqa: BLE001 - one check failing must not stop the rest
            logger.exception("supervisory check %s failed", name)
            failed.append(name)
    try:
        judged = followup.follow_up()
    except Exception:  # noqa: BLE001 - the next tick tries again
        logger.exception("supervisory follow-up failed")
        judged = None
        failed.append("follow_up")
    _write_state(now, now + timedelta(seconds=interval), failed)
    return {
        "medic_read": incidents is not None,
        "swept": swept,
        "proposed": proposed,
        "followed_up": judged,
        "failed": failed,
    }


def _read_state() -> SupervisoryWorkerState | None:
    with unit_of_work() as session:
        row = session.execute(select(SupervisoryWorkerState)).scalar_one_or_none()
        if row is not None:
            session.expunge(row)
        return row


def _write_state(at: datetime, next_at: datetime, failed: list[str]) -> None:
    values = {
        "last_check_at": at,
        "next_check_at": next_at,
        "last_error": ", ".join(failed),
    }
    with unit_of_work() as session:
        session.execute(
            insert(SupervisoryWorkerState)
            .values(id=1, **values)
            .on_conflict_do_update(index_elements=["id"], set_=values)
        )


def status(
    medic_open_related: int,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The page's state, and when the check last ran and runs next.

    - off: switched off on this install. Nothing is read, and no times are given.
    - unknown: the state row could not be read, there is none yet (no check has
      run), or the last check is more than two intervals old, so no daemon is
      checking on schedule.
    - on_hold: a related Medic incident is open and holds a suggestion.
    - running: checking on schedule.
    """
    settings = settings or get_settings()
    now = now or datetime.now(UTC)
    if not enabled(settings):
        return {"state": "off", "last_check_at": None, "next_check_at": None}
    try:
        state = _read_state()
    except Exception:  # noqa: BLE001 - the page says unknown rather than failing
        logger.exception("could not read the supervisory worker state")
        return {"state": "unknown", "last_check_at": None, "next_check_at": None}
    if state is None:
        return {"state": "unknown", "last_check_at": None, "next_check_at": None}
    stale = now - state.last_check_at > 2 * timedelta(
        seconds=interval_seconds(settings)
    )
    if stale:
        word = "unknown"
    elif medic_open_related > 0:
        word = "on_hold"
    else:
        word = "running"
    return {
        "state": word,
        "last_check_at": state.last_check_at.isoformat(),
        "next_check_at": state.next_check_at.isoformat(),
    }
