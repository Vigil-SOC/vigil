"""Findings the enrichment sweep has yet to rate, and ones it has given up on."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import false, func, not_, select, text

from core.storage.models import Finding
from core.storage.models.finding import UNRATED_WHERE
from core.time import utcnow

# Stored more recently than this, a Finding is still in ordinary triage.
WAITING_AFTER = timedelta(minutes=10)


def unrated():
    return text(UNRATED_WHERE)


def count_unrated(
    session: Any, max_age_hours: Optional[int], now: Optional[datetime] = None
) -> Dict[str, int]:
    """Unrated Findings within the sweep's max age, and older. Raises on failure."""
    now = now or utcnow()
    past_max_age = (
        Finding.created_at < now - timedelta(hours=max_age_hours)
        if max_age_hours
        else false()
    )
    waiting, never_rated = session.execute(
        select(
            func.count().filter(not_(past_max_age)),
            func.count().filter(past_max_age),
        ).where(unrated(), Finding.created_at < now - WAITING_AFTER)
    ).one()
    return {"waiting_to_be_rated": waiting, "never_rated": never_rated}
