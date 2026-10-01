"""Findings the enrichment sweep has yet to rate, and ones it has given up on."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import func, literal, select, text

from core.storage.models import Finding
from core.storage.models.finding import UNRATED_WHERE
from core.time import utcnow

# Stored more recently than this, a Finding is still in ordinary triage.
WAITING_AFTER = timedelta(minutes=10)

# never_rated only grows; past this it reads as the cap, so a count stays cheap.
NEVER_RATED_CAP = 100_000


def unrated():
    return text(UNRATED_WHERE)


def _in_sweep_index():
    # Pins the index's first column so Postgres 16 range-scans created_at, its
    # second. NULL is only the moment between migrate's column and marking steps.
    return Finding.bulk_imported.in_([False, True])


def count_unrated(
    session: Any, max_age_hours: Optional[int], now: Optional[datetime] = None
) -> Dict[str, Any]:
    """Unrated Findings within the sweep's max age, and older. Raises on failure."""
    now = now or utcnow()
    cutoff = now - timedelta(hours=max_age_hours) if max_age_hours else None

    waiting_where = [
        unrated(),
        _in_sweep_index(),
        Finding.created_at < now - WAITING_AFTER,
    ]
    if cutoff is not None:
        waiting_where.append(Finding.created_at >= cutoff)
    waiting = session.execute(
        select(func.count()).select_from(Finding).where(*waiting_where)
    ).scalar_one()

    never_rated = 0
    if cutoff is not None:
        past_max_age = (
            select(literal(1))
            .select_from(Finding)
            .where(unrated(), _in_sweep_index(), Finding.created_at < cutoff)
            .limit(NEVER_RATED_CAP + 1)
            .subquery()
        )
        never_rated = session.execute(
            select(func.count()).select_from(past_max_age)
        ).scalar_one()

    return {
        "waiting_to_be_rated": waiting,
        "never_rated": min(never_rated, NEVER_RATED_CAP),
        "never_rated_capped": never_rated > NEVER_RATED_CAP,
    }
