"""What every Monitor number carries: its value, its limit, a level word, and an
``info`` line saying where it comes from and how it is counted.

The console renders these as-is (``MonitorMetric`` in ``models.py``). Levels follow the console PRD: usage
against a limit is good under 75%, fair from 75% to 90%, poor above; rates are
good at 95% or better, fair from 85%, poor below. A number with no limit has no
level. A number Vigil does not record yet is ``None`` with ``unmeasured`` text,
never a guessed zero.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

UNMEASURED = "Not measured yet"
USAGE_FAIR = 0.75
USAGE_POOR = 0.90
RATE_GOOD = 0.95
RATE_FAIR = 0.85


class NotImplementedYet(NotImplementedError):
    """A Monitor read whose data source is not wired yet. The router answers 501."""


def usage_level(used: Optional[float], limit: Optional[float]) -> Optional[str]:
    if used is None or not limit:
        return None
    share = used / limit
    if share > USAGE_POOR:
        return "poor"
    if share >= USAGE_FAIR:
        return "fair"
    return "good"


def rate_level(rate: Optional[float]) -> Optional[str]:
    if rate is None:
        return None
    if rate >= RATE_GOOD:
        return "good"
    if rate >= RATE_FAIR:
        return "fair"
    return "poor"


def metric(
    value: Any,
    *,
    info: str,
    limit: Any = None,
    level: Optional[str] = None,
    unmeasured: Optional[str] = None,
    **extra: Any,
) -> dict:
    """One number as the console shows it. ``unmeasured`` names why it is absent."""
    return {
        "value": None if unmeasured else value,
        "limit": limit,
        "level": None if unmeasured else level,
        "info": info,
        "unmeasured_text": UNMEASURED if unmeasured else None,
        "unmeasured_reason": unmeasured,
        **extra,
    }


def day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def same_time_yesterday(now: datetime) -> tuple[datetime, datetime]:
    """Yesterday from midnight to this time of day: the pace comparison."""
    start = day_start(now) - timedelta(days=1)
    return start, now - timedelta(days=1)
