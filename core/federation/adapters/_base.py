"""Shared helpers for federation adapters."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional, Tuple

from core.time import utcnow

logger = logging.getLogger(__name__)

# A drained window's cursor stops this far short of the fetch time, so an alert
# the source had not finished indexing when we asked is read on the next tick
# instead of falling behind the cursor. The overlap is re-read and deduped.
SETTLING_MARGIN = timedelta(minutes=1)

# Fractional seconds beyond microseconds: Defender emits seven digits, which
# datetime.fromisoformat rejects.
_EXTRA_FRACTION = re.compile(r"^(.*?\.\d{6})\d+(.*)$")


def parse_cursor_since(cursor: Dict[str, Any]) -> Optional[datetime]:
    """Read the ``last_poll_at`` ISO timestamp from cursor, if present.

    Returns ``None`` for first-run (empty cursor) — adapters MUST treat that
    as "from now" rather than backfilling, per the federation MVP design.
    """
    raw = cursor.get("last_poll_at") if cursor else None
    if not raw:
        return None
    try:
        # Drop trailing Z if present (datetime.fromisoformat doesn't accept it
        # before 3.11 in all cases).
        if isinstance(raw, str) and raw.endswith("Z"):
            raw = raw[:-1]
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def fresh_cursor() -> Dict[str, Any]:
    """Cursor value to persist after a fetch that drained its window."""
    return {"last_poll_at": utcnow().isoformat()}


def drained_cursor(now: datetime) -> Dict[str, Any]:
    """Cursor value after a fetch that read its whole window, taken at ``now``."""
    return cursor_at(now - SETTLING_MARGIN)


def next_cursor(
    times: Iterable[Optional[datetime]],
    *,
    truncated: bool,
    start: datetime,
    now: datetime,
    step: timedelta,
    source: str,
    ids: Optional[Iterable[Optional[str]]] = None,
    after: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Where the next tick starts, and whether the source may hold more past it.

    ``times`` are the returned alerts' times as naive UTC (None if unreadable),
    ``start`` the tick's cursor and ``now`` the clock taken before the fetch.
    A short batch drained its window. A full batch stops at the newest alert
    returned, capped at ``now`` so a source clock ahead of ours cannot carry the
    cursor into the future; the next tick re-reads that boundary alert (start
    filters are inclusive) and dedup absorbs it. A full batch sitting entirely
    at or before ``start`` steps past it, skipping whatever at that instant did
    not fit, so the tick cannot fetch the same page forever.

    ``ids`` (parallel to ``times``) is for a source that can resume strictly
    after one alert: its full batch stores the last alert's ID beside the
    time, and the next fetch starts after that alert instead of at the
    instant, so a burst sharing one timestamp is paged rather than stepped
    over. ``after`` is the ID the tick started from.
    """
    if not truncated:
        return drained_cursor(now), False

    times = list(times)
    newest = max((t for t in times if t is not None), default=None)
    if newest is None:
        logger.warning(
            "Federation %s: batch filled max_items but no alert carried a readable "
            "time; cursor moves to now and the rest of the window is skipped",
            source,
        )
        return drained_cursor(now), False

    if newest > now:
        logger.warning(
            "Federation %s: newest alert time %s is ahead of this host's clock %s; "
            "capping the cursor at the clock",
            source,
            newest.isoformat(),
            now.isoformat(),
        )
        return cursor_at(now), True

    if ids is not None:
        at_newest = [i for t, i in zip(times, ids) if t == newest and i is not None]
        tie = max(at_newest, default=None)
        if tie is not None and (newest > start or after is None or tie > after):
            return {**cursor_at(newest), "after_id": tie}, True

    if newest <= start:
        logger.warning(
            "Federation %s: batch filled max_items with newest alert time %s not "
            "past the tick start %s; stepping the cursor to %s. Alerts at that "
            "instant beyond this batch are skipped",
            source,
            newest.isoformat(),
            start.isoformat(),
            (start + step).isoformat(),
        )
        newest = start + step

    return cursor_at(newest), True


def cursor_at(when: datetime) -> Dict[str, Any]:
    """Cursor value that resumes from ``when`` (naive UTC) rather than now.

    Used when a fetch filled ``max_items``: the window past the newest returned
    alert has not been read yet, so the next tick must start there.
    """
    return {"last_poll_at": when.isoformat()}


def parse_alert_time(raw: Any) -> Optional[datetime]:
    """Normalise a vendor timestamp to the naive UTC ``parse_cursor_since`` returns.

    Accepts an aware or naive ``datetime`` or an ISO-8601 string with a
    trailing ``Z``, a UTC offset, or more than six fractional digits. Returns
    ``None`` for anything unreadable so a caller can fall back rather than fail
    a whole poll on one malformed record.
    """
    if isinstance(raw, datetime):
        parsed = raw
    elif isinstance(raw, str) and raw.strip():
        text = raw.strip()
        if text[-1] in "Zz":
            text = text[:-1] + "+00:00"
        text = _EXTRA_FRACTION.sub(r"\1\2", text)
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed
