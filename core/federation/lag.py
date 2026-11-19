"""How long since each federation source last pulled successfully."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from core.time import utcnow


def overdue(
    last: Optional[datetime],
    interval_seconds: float,
    now: datetime,
    *,
    slack: float = 0,
) -> bool:
    """True when ``last`` is older than one poll interval (plus ``slack``) at ``now``.

    The per-interval judgement of #4864: a source is held to its own interval,
    not a fixed age. A missing ``last`` is overdue.
    """
    if last is None:
        return True
    if last.tzinfo is not None:
        last = last.replace(tzinfo=None)
    return (now - last).total_seconds() > interval_seconds + slack


def source_collection_lag(now: Optional[datetime] = None) -> list[dict]:
    """``now - last_success_at`` per ``federation_sources`` row.

    ``last_success_at`` comes from the source's Integration Health record.
    Quiet when it is null, or the age exceeds ``interval_seconds``. A quiet
    source is one the poller has not kept up.
    """
    from core.federation.store import read_sources
    from core.integrations.health import health_id, read_all

    if now is None:
        now = utcnow()
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    records = read_all()
    result = []
    for row in sorted(read_sources(with_health=False), key=lambda r: r["source_id"]):
        success = (records.get(health_id(row["source_id"])) or {}).get(
            "last_success_at"
        )
        if success is not None and success.tzinfo is not None:
            success = success.replace(tzinfo=None)
        interval = int(row["interval_seconds"])
        result.append(
            {
                "source_id": row["source_id"],
                "lag_seconds": (
                    None if success is None else (now - success).total_seconds()
                ),
                "quiet": overdue(success, interval, now),
                "interval_seconds": interval,
            }
        )
    return result
