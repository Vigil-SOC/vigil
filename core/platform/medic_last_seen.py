"""The backend's last-seen poller for Medic (C5 §5.3, D2-18).

Medic may not call the backend, so the backend calls Medic: ``GET /v1/status``
(X2) through the gateway's inbound listener, with the per-install
``X-Medic-Key``, once a minute. The answer goes into the one ``medic_last_seen``
row, so it survives a backend restart and every replica reads the same thing.

One poller across replicas: each backend runs the loop, and each tick takes a
transaction-scoped Postgres advisory lock (the bootstrap's mechanism,
``services/api/routers/auth.py``, in its non-blocking form) and skips if another
replica holds it or polled within the interval. All times are the backend's
clock; Medic's own clock is never stored.

Flag off: no task, no poll, no row (Off is not Down, C8).

After a poll, this process starts one watch. The watch reads the row this
poller wrote, and under its own lock tells holders of medic.admin when Medic
has stopped watching or is watching again. It does not call Medic, and it
sends the mail only after that lock commits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from sqlalchemy import text

from core.config import Settings, get_settings
from core.platform import notices
from core.platform.medic_status import (
    POLL_INTERVAL_S,
    POLL_TIMEOUT_S,
    LastSeen,
    MedicStatus,
    medic_enabled,
    medic_status,
)

logger = logging.getLogger(__name__)

# Arbitrary constant identifying the poller's advisory lock.
_POLL_LOCK = 8_274_120
# The watch's own lock. It never waits, and it is not held across the send.
_WATCH_LOCK = 8_274_121
# A replica whose own timer fires within this much of the last poll skips it.
_FRESH = timedelta(seconds=POLL_INTERVAL_S - 5)
# Unwritten this long, the row's failure is history (as medic_status reads it).
_STALE = timedelta(seconds=3 * POLL_INTERVAL_S)
REQUEST_TIMEOUT_S = float(POLL_TIMEOUT_S)
SNAPSHOT_MAX_BYTES = 2048
_MAX_BODY = 64 * 1024

# X2: 32 random bytes, base64url, no padding. The gateway checks the same shape.
_KEY = re.compile(r"[A-Za-z0-9_-]{43}")
_TS = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z"
)
_INSTANCE = re.compile(r"mi_[0-9a-f]{16}")
_API_VERSION = re.compile(r"2\.[0-9]{1,3}")
_STATES = frozenset(
    {"running", "degraded", "not_recording", "blind", "starting", "crash_looping"}
)
# Red is told after two polls of that same word. Watching polls mix.
_RED = frozenset({"down", "blind", "not_recording", "crash_looping"})
_WATCHING = frozenset({"running", "degraded"})
_CONFIRM = 2
_MEDIC_LINK = "/medic"
_NOTICE_KIND = "medic_status"
_STOPPED = {
    "down": "Medic stopped watching (Down).",
    "blind": "Medic stopped watching (Blind).",
    "not_recording": "Medic stopped watching (Not recording).",
    "crash_looping": "Medic stopped watching (Crash-looping).",
}
_WATCHING_AGAIN = "Medic is watching again."
# One watch task in this process. A second start while it runs does nothing.
_watch_task: Optional[asyncio.Task] = None


def _no_holders() -> list[str]:
    return []


# Who a status notice is for. core.auth binds this: this tier cannot import it.
_medic_admin_holders: Callable[[], list[str]] = _no_holders
_EXIT_REASONS = frozenset(
    {"clean", "signal", "oom", "watchdog_stall", "crash", "unknown"}
)
_INT_MAX = 2**53


class FailureKind(str, Enum):
    REFUSED = "refused"
    TIMEOUT = "timeout"
    UNAUTHORIZED = "401"
    SERVER_ERROR = "5xx"


@dataclass(frozen=True)
class Outcome:
    snapshot: Optional[dict]
    failure: Optional[FailureKind]
    # The HTTP status behind a failure (the kinds are closed; 429 reads 5xx).
    http_status: Optional[int] = None


Fetch = Callable[[str, Optional[str]], Outcome]


def _fail(kind: FailureKind, http_status: Optional[int] = None) -> Outcome:
    return Outcome(snapshot=None, failure=kind, http_status=http_status)


def read_key(settings: Settings) -> Optional[str]:
    """The API key from its file, or None if it's missing or not X2's shape."""
    path = settings.vigil_medic_api_key_file
    if not path:
        return None
    try:
        key = Path(path).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return key if _KEY.fullmatch(key) else None


def _int(value: Any) -> int:
    if type(value) is not int or not 0 <= value < _INT_MAX:
        raise ValueError
    return value


def _match(pattern: re.Pattern, value: Any) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError
    return value


def _enum(allowed: frozenset, value: Any) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError
    return value


def _bool(value: Any) -> bool:
    if type(value) is not bool:
        raise ValueError
    return value


def typed_snapshot(body: Any) -> Optional[dict]:
    """Medic's status reduced to typed fields (K1 §6 G2), or None if it isn't one.

    Medic's ``now`` is dropped (the backend's clock only); the store, pack and
    chain blocks are left to the console's live proxy (H3).
    """
    try:
        last_exit = body["last_exit"]
        sensors = body["sensors"]
        snap = {
            "api_version": _match(_API_VERSION, body["api_version"]),
            "instance_id": _match(_INSTANCE, body["instance_id"]),
            "state": _enum(_STATES, body["state"]),
            "heartbeat_at": _match(_TS, body["heartbeat_at"]),
            "started_at": _match(_TS, body["started_at"]),
            "cycle": _int(body["cycle"]),
            "uptime_s": _int(body["uptime_s"]),
            "restarts_24h": _int(body["restarts_24h"]),
            "dev_mode": _bool(body["dev_mode"]),
            "last_exit": (
                None
                if last_exit is None
                else {
                    "reason": _enum(_EXIT_REASONS, last_exit["reason"]),
                    "at": _match(_TS, last_exit["at"]),
                }
            ),
            "sensors": {k: _int(sensors[k]) for k in ("reporting", "cant_see", "off")},
        }
    except (KeyError, TypeError, ValueError):
        return None
    if len(snapshot_bytes(snap)) > SNAPSHOT_MAX_BYTES:
        return None
    return snap


def snapshot_bytes(snap: dict) -> bytes:
    return json.dumps(snap, sort_keys=True, separators=(",", ":")).encode()


def _classify(response: httpx.Response) -> Outcome:
    status = response.status_code
    if status in (401, 403):
        return _fail(FailureKind.UNAUTHORIZED, status)
    if status != 200:
        # The gateway reports the hop to Medic as problem JSON with a closed code.
        try:
            code = response.json().get("code")
        except (ValueError, AttributeError):
            code = None
        if code == "upstream_unreachable":
            return _fail(FailureKind.REFUSED, status)
        if code == "upstream_timeout":
            return _fail(FailureKind.TIMEOUT, status)
        return _fail(FailureKind.SERVER_ERROR, status)
    # Read whole by httpx first; the gateway caps what it relays at 4 MiB.
    if len(response.content) > _MAX_BODY:
        return _fail(FailureKind.SERVER_ERROR, status)
    try:
        snap = typed_snapshot(response.json())
    except ValueError:
        snap = None
    if snap is None:
        return _fail(FailureKind.SERVER_ERROR, status)
    return Outcome(snapshot=snap, failure=None)


def fetch_status(
    url: str,
    key: Optional[str],
    *,
    timeout: float = REQUEST_TIMEOUT_S,
    transport: Optional[httpx.BaseTransport] = None,
) -> Outcome:
    """One ``GET /v1/status``. Never raises; never logs the key or the body."""
    if not url:
        return _fail(FailureKind.REFUSED)  # no path to Medic is configured
    if key is None:
        return _fail(FailureKind.UNAUTHORIZED)  # nothing Medic would accept
    try:
        with httpx.Client(
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.get(
                url.rstrip("/") + "/v1/status", headers={"X-Medic-Key": key}
            )
    except httpx.TimeoutException:
        return _fail(FailureKind.TIMEOUT)
    except httpx.TransportError:
        return _fail(FailureKind.REFUSED)
    return _classify(response)


def poll_once(
    settings: Optional[Settings] = None,
    *,
    now: Optional[datetime] = None,
    fetch: Optional[Fetch] = None,
) -> bool:
    """Poll Medic and record the result, unless another replica just did.

    Returns True if this call polled. The lock is held across the request
    (``REQUEST_TIMEOUT_S`` per connect and per read), so a replica that ticks meanwhile skips. Times
    are Postgres's ``now()``, so every replica reads and writes one clock
    (still the backend's side, never Medic's); ``now`` overrides it in tests.
    """
    from core.storage.connection import get_db_session
    from core.storage.models import MedicLastSeen

    settings = settings or get_settings()
    if not medic_enabled(settings):
        return False
    fetch = fetch or fetch_status
    with get_db_session() as session, session.begin():
        locked = session.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _POLL_LOCK}
        ).scalar()
        if not locked:
            return False
        now = now or _db_now(session)
        row = session.get(MedicLastSeen, 1)
        if row is not None and now - row.updated_at < _FRESH:
            return False
        outcome = fetch(settings.vigil_medic_api_url, read_key(settings))
        if row is None:
            row = MedicLastSeen(id=1, updated_at=now)
            session.add(row)
        elif now - row.updated_at >= _STALE:
            # Nobody polled for a while (Medic was off): an old failure is not
            # the start of this one.
            row.first_failed_at = None
            row.failure_kind = None
        row.updated_at = now
        if outcome.failure is None:
            if row.first_failed_at is not None:
                logger.info("Medic status poll succeeded again")
            row.last_seen_at = now
            row.first_failed_at = None
            row.failure_kind = None
            row.status_snapshot = outcome.snapshot
        else:
            if row.first_failed_at is None:
                logger.warning(
                    "Medic status poll failed: %s (HTTP %s)",
                    outcome.failure.value,
                    outcome.http_status or "-",
                )
                row.first_failed_at = now
            row.failure_kind = outcome.failure.value
    return True


def _db_now(session) -> datetime:
    return session.execute(text("SELECT now() AT TIME ZONE 'UTC'")).scalar_one()


def read_last_seen() -> tuple[Optional[LastSeen], datetime]:
    """The row (None before the first poll) and Postgres's time, read together."""
    from core.storage.connection import get_db_session
    from core.storage.models import MedicLastSeen

    with get_db_session() as session, session.begin():
        now = _db_now(session)
        row = session.get(MedicLastSeen, 1)
        if row is None:
            return None, now
        return (
            LastSeen(
                last_seen_at=row.last_seen_at,
                first_failed_at=row.first_failed_at,
                failure_kind=row.failure_kind,
                updated_at=row.updated_at,
                status_snapshot=row.status_snapshot,
            ),
            now,
        )


def current_status(
    settings: Optional[Settings] = None, now: Optional[datetime] = None
) -> MedicStatus:
    """Off, running, down or unknown; unknown if the row can't be read."""
    settings = settings or get_settings()
    if not medic_enabled(settings):
        return MedicStatus.OFF
    try:
        seen, db_now = read_last_seen()
    except Exception:  # noqa: BLE001 - Postgres down: the console can't tell
        return MedicStatus.UNKNOWN
    return medic_status(settings, seen, now or db_now)


def _state_word(settings: Settings, seen: Optional[LastSeen], now: datetime) -> str:
    """The status-line word. off, down and unknown win over the snapshot."""
    if not medic_enabled(settings):
        return MedicStatus.OFF.value
    status = medic_status(settings, seen, now)
    if status is not MedicStatus.RUNNING:
        return status.value
    state = (seen.status_snapshot or {}).get("state") if seen else None
    return state if state in _STATES else MedicStatus.UNKNOWN.value


def current_state_word(settings: Optional[Settings] = None) -> str:
    """The one word for the status line: off, down or unknown win; otherwise
    Medic's stored snapshot state. One row read, so the two can't disagree. A
    missing or unrecognised snapshot state is unknown, never running."""
    settings = settings or get_settings()
    if not medic_enabled(settings):
        return MedicStatus.OFF.value
    try:
        seen, db_now = read_last_seen()
    except Exception:  # noqa: BLE001 - Postgres down: the console can't tell
        return MedicStatus.UNKNOWN.value
    return _state_word(settings, seen, db_now)


def _seen_of(row) -> LastSeen:
    return LastSeen(
        last_seen_at=row.last_seen_at,
        first_failed_at=row.first_failed_at,
        failure_kind=row.failure_kind,
        updated_at=row.updated_at,
        status_snapshot=row.status_snapshot,
    )


def _count(state, word: str, updated_at: datetime) -> Optional[str]:
    """Count this poll once. The sentence to send, or None.

    starting, unknown and off leave the count. Two polls of one red word
    send "stopped watching"; a different red word starts again. Two watching
    polls, running and degraded in either mix, send "watching again" only
    when the announced word is red. The first watching state sends nothing.
    """
    state.counted_updated_at = updated_at
    if word not in _RED and word not in _WATCHING:
        return None
    same = state.confirming_word == word or (
        state.confirming_word in _WATCHING and word in _WATCHING
    )
    if same:
        state.confirming_word = word
        if state.confirming_polls < _CONFIRM:
            state.confirming_polls += 1
    else:
        state.confirming_word = word
        state.confirming_polls = 1
        if word in _RED:
            state.red_since = updated_at
    if state.confirming_polls < _CONFIRM:
        return None
    if word in _RED and state.announced_word != word:
        state.announced_word = word
        return _STOPPED[word]
    if word in _WATCHING and state.announced_word in _RED:
        state.announced_word = word
        state.red_since = None
        return _WATCHING_AGAIN
    return None


def watch_once(settings: Optional[Settings] = None) -> list[int]:
    """Confirm the polled word and write any notice. Does not send it.

    The lock is the transaction, so it drops at commit, before the caller
    sends. No connection to Medic: the word comes from the last-seen row.
    A poll already counted, or a lock another replica holds, writes nothing.
    """
    from core.storage.connection import get_db_session
    from core.storage.models import MedicLastSeen, MedicWatchState

    settings = settings or get_settings()
    with get_db_session() as session, session.begin():
        locked = session.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _WATCH_LOCK}
        ).scalar()
        if not locked:
            return []
        now = _db_now(session)
        row = session.get(MedicLastSeen, 1)
        state = session.get(MedicWatchState, 1)
        if state is None:
            state = MedicWatchState(id=1, confirming_polls=0)
            session.add(state)
        if row is None or state.counted_updated_at == row.updated_at:
            return []
        sentence = _count(
            state, _state_word(settings, _seen_of(row), now), row.updated_at
        )
        if sentence is None:
            return []
        ids = [
            notices.notify(
                principal,
                _NOTICE_KIND,
                sentence,
                sentence,
                link=_MEDIC_LINK,
                session=session,
            )
            for principal in _medic_admin_holders()
        ]
        logger.info("Medic status notice to %s holder(s): %s", len(ids), sentence)
        return ids


def bind_medic_admin_holders(holders: Callable[[], list[str]]) -> None:
    """The principal ids a status notice goes to, read at send time."""
    global _medic_admin_holders
    _medic_admin_holders = holders


def not_watching_line() -> Optional[dict[str, str]]:
    """The bell line while the announced word is red, or None.

    Hidden while Medic is off. It reads the state row and does not call Medic.
    The permission check is the caller's: this tier cannot import it.
    """
    if not medic_enabled():
        return None
    from core.storage.models import MedicWatchState
    from core.storage.unit_of_work import unit_of_work

    with unit_of_work() as session:
        state = session.get(MedicWatchState, 1)
        if state is None or state.announced_word not in _RED or state.red_since is None:
            return None
        when = state.red_since.strftime("%H:%M")
    return {"message": f"Medic isn't watching since {when}", "link": _MEDIC_LINK}


def _kick_watch() -> None:
    """Start the watch. A watch already running in this process is left alone."""
    global _watch_task
    if _watch_task is not None and not _watch_task.done():
        return
    _watch_task = asyncio.get_running_loop().create_task(_run_watch())


async def _run_watch() -> None:
    try:
        notice_ids = await asyncio.to_thread(watch_once)
    except Exception as exc:  # noqa: BLE001 - the next kick tries again
        logger.warning("Medic watch failed: %s", type(exc).__name__)
        return
    if not notice_ids:
        return
    # After the watch's commit. The lock is already gone.
    try:
        await asyncio.to_thread(notices.send_notices, notice_ids)
    except Exception as exc:  # noqa: BLE001 - the rows are committed
        logger.warning("Medic watch send failed: %s", type(exc).__name__)


def _cancel_watch() -> None:
    if _watch_task is not None and not _watch_task.done():
        _watch_task.cancel()


async def run_poller(settings: Settings, interval: float = POLL_INTERVAL_S) -> None:
    """A fixed cadence: a slow (timed-out) poll doesn't push the next one back.

    Cancelling it at shutdown leaves an in-flight poll to finish in its thread
    (``REQUEST_TIMEOUT_S`` per connect and read); its transaction then commits or rolls back.
    """
    loop = asyncio.get_running_loop()
    due = loop.time()
    try:
        while True:
            polled = False
            try:
                polled = await asyncio.to_thread(poll_once, settings)
            except Exception as exc:  # noqa: BLE001 - the next tick tries again
                logger.warning("Medic status poll skipped: %s", type(exc).__name__)
            # Not awaited: a watch must not hold up the next poll.
            if polled:
                _kick_watch()
            # After a stall (a hung connect, a suspended host) skip the missed ticks
            # rather than run them back to back.
            due = max(due + interval, loop.time())
            await asyncio.sleep(max(0.0, due - loop.time()))
    finally:
        _cancel_watch()


def start_poller(settings: Optional[Settings] = None) -> Optional[asyncio.Task]:
    """Start the loop in this backend process; None (no task) with the flag off."""
    settings = settings or get_settings()
    if not medic_enabled(settings):
        return None
    # Polls would still run and read Down; say why once, never the key.
    if not settings.vigil_medic_api_url:
        logger.warning("Medic is on but VIGIL_MEDIC_API_URL is empty: Medic reads Down")
    if read_key(settings) is None:
        logger.warning(
            "Medic is on but VIGIL_MEDIC_API_KEY_FILE is missing, unreadable or "
            "not an X2 key: Medic reads Down"
        )
    logger.info("Medic status poller started (every %ss)", POLL_INTERVAL_S)
    return asyncio.get_running_loop().create_task(run_poller(settings))
