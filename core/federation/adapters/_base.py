"""Shared helpers for federation adapters."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional

from core.time import utcnow

logger = logging.getLogger(__name__)

# How far past a stuck instant the cursor steps. Elastic's ``date`` fields and
# Security Hub's ``CreatedAt`` resolve to the millisecond, so a smaller step
# would round back to the same instant on their side and re-read the same page
# every tick; Defender and Sentinel compare finer than this and lose nothing
# they were not already losing at that instant.
CURSOR_STEP = timedelta(milliseconds=1)

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


def cursor_at(when: datetime) -> Dict[str, Any]:
    """Cursor value that resumes from ``when`` (naive UTC) rather than now.

    Used when a fetch filled ``max_items``: the window past the newest returned
    alert has not been read yet, so the next tick must start there.
    """
    return {"last_poll_at": when.isoformat()}


def full_batch_cursor(
    times: Iterable[Optional[datetime]],
    *,
    start: datetime,
    now: datetime,
    source: str,
    count: int,
) -> Optional[Dict[str, Any]]:
    """Cursor after a batch that filled ``max_items`` (#1233).

    ``times`` are the returned items' naive-UTC times (``None`` if unreadable).
    The cursor stops at the newest one, capped at ``now`` (taken before the
    fetch) so a future-stamped item cannot carry it ahead of the clock. The next
    tick re-reads that boundary item (start filters are inclusive) and dedup
    absorbs it. Returns ``None`` when no item had a readable time; the caller
    then falls back to its drained cursor.
    """
    newest = max((t for t in times if t is not None), default=None)
    if newest is None:
        logger.warning(
            "Federation %s: batch filled max_items=%d but no alert carried a "
            "readable time; cursor moves to now and the rest of the window "
            "is skipped",
            source,
            count,
        )
        return None

    if newest > now:
        logger.warning(
            "Federation %s: newest alert time %s is ahead of this host's clock "
            "(window end %s); capping the cursor there",
            source,
            newest.isoformat(),
            now.isoformat(),
        )
        newest = now

    if newest <= start:
        # Every item in a full batch sits at or before the tick's start. Step
        # just past that instant so the next tick cannot fetch the same page
        # forever; items at the instant beyond this batch are skipped.
        logger.warning(
            "Federation %s: batch filled max_items=%d with newest alert time "
            "%s not past the tick start %s; stepping the cursor to %s. Alerts "
            "at that instant beyond this batch are skipped",
            source,
            count,
            newest.isoformat(),
            start.isoformat(),
            (start + CURSOR_STEP).isoformat(),
        )
        newest = start + CURSOR_STEP

    return cursor_at(newest)


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
