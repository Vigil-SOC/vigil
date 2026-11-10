"""When an account has been idle too long (#4176).

An account's last activity is the latest of its last complete sign-in, the last use
of one of its personal MCP credentials, its creation, and its ``dormancy_floor``
(the upgrade, or its last reactivation). Computed on read; nothing is written per
request. A failed sign-in, a lock, and an agent run's token (which is not an MCP
credential) are not activity. The policy's ``dormancy_days`` is the idle period;
the sweep in ``services/api/dormancy.py`` warns, then deactivates.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.storage.models import McpCredential, User

# How long before the deadline the holder and managers are warned; never more
# than the period leaves.
WARN_DAYS = 7


def activity(session: Session, user_ids: Optional[Iterable[str]] = None) -> dict:
    """User id to last activity. GREATEST ignores a missing value."""
    used = (
        select(func.max(McpCredential.last_used_at))
        .where(McpCredential.user_id == User.user_id)
        .scalar_subquery()
    )
    query = select(
        User.user_id,
        func.greatest(User.last_login, used, User.created_at, User.dormancy_floor),
    )
    if user_ids is not None:
        query = query.where(User.user_id.in_(list(user_ids)))
    return dict(session.execute(query).all())


def deadline(last: datetime, days: int) -> datetime:
    return last + timedelta(days=days)


def window(days: int) -> timedelta:
    return timedelta(days=min(WARN_DAYS, days - 1))


def warning_start(last: datetime, days: int) -> datetime:
    return deadline(last, days) - window(days)
