"""Today's finding arrivals, grouped by source.

Overview and the Triage strip both call this. "Today" is the UTC calendar
day on the naive timestamps ``utcnow()`` already writes.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import func

from core.storage.connection import get_db_manager
from core.storage.models import FederationSource, Finding
from core.time import utcnow


def utc_day_bounds(day: Optional[date] = None) -> tuple[datetime, datetime]:
    """``[start, end)`` for one UTC calendar day, naive like the columns."""
    if day is None:
        day = utcnow().date()
    start = datetime(day.year, day.month, day.day)
    return start, start + timedelta(days=1)


def arrivals_today_by_source(day: Optional[date] = None) -> list[dict]:
    """Findings with ``created_at`` on that day, grouped by ``data_source``.

    An enabled federation source with no arrivals is still a source, at
    zero. The group-by alone would drop it.
    """
    start, end = utc_day_bounds(day)
    db = get_db_manager()
    with db.session_scope() as session:
        grouped = (
            session.query(Finding.data_source, func.count(Finding.finding_id))
            .filter(Finding.created_at >= start, Finding.created_at < end)
            .group_by(Finding.data_source)
            .all()
        )
        enabled = {
            row[0]
            for row in session.query(FederationSource.source_id)
            .filter(FederationSource.enabled.is_(True))
            .all()
        }
    counts = {source: int(count) for source, count in grouped}
    names = sorted(set(counts) | enabled)
    return [{"data_source": name, "count": counts.get(name, 0)} for name in names]
