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
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from core.storage.models import McpCredential, SystemConfig, User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

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


# A restore brings back last-sign-in dates as backed up, so for this long after it
# settled the sweep deactivates nobody and starts no warnings. Its own row, apart
# from the session policy, which is edited and validated as a document.
GRACE = timedelta(days=7)
GRACE_KEY = "auth.restore_settled_at"


def start_grace(session: Session, now: Optional[datetime] = None) -> datetime:
    """Record that a restore settled now; a later restore overwrites it."""
    now = now or utcnow()
    value = {"settled_at": now.isoformat()}
    session.execute(
        insert(SystemConfig)
        .values(
            key=GRACE_KEY,
            value=value,
            description="When the last restore settled: dormancy's grace counts from it",
            config_type="auth",
            updated_by="restore",
        )
        .on_conflict_do_update(index_elements=["key"], set_={"value": value})
    )
    return now


def grace_until() -> Optional[datetime]:
    """When the grace after the last restore ends, or None if no restore recorded."""
    with unit_of_work() as session:
        value = session.query(SystemConfig.value).filter(
            SystemConfig.key == GRACE_KEY
        ).scalar()
    if not value:
        return None
    return datetime.fromisoformat(value["settled_at"]) + GRACE


def in_grace(now: Optional[datetime] = None) -> bool:
    until = grace_until()
    return until is not None and (now or utcnow()) < until
