"""Live bell line for the shared IOC index's catch-up (#4734, epic #4580).

Computed on each request, stored nowhere, and gone when the count reaches zero.
Shown to the same admins as the insecure-cookie line (``platform.write``).
"""

from __future__ import annotations

from sqlalchemy import func, select

from core.auth.permissions import missing_permission
from core.platform import notices
from core.storage.indicator_catchup import LAG_LIMIT
from core.storage.models import Case
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

LINK = "/cases"


def _count(*where) -> int:
    with unit_of_work() as session:
        return session.scalar(select(func.count()).select_from(Case).where(*where)) or 0


def _plural(n: int) -> str:
    return f"{n} Case has" if n == 1 else f"{n} Cases have"


def waiting_too_long(user) -> list[dict[str, str]]:
    """Cases whose indicators have waited past the limit."""
    if missing_permission(user.user_id, "platform.write") is not None:
        return []
    n = _count(Case.indicators_stale_since < utcnow() - LAG_LIMIT)
    if not n:
        return []
    minutes = int(LAG_LIMIT.total_seconds() // 60)
    return [
        {
            "message": f"{_plural(n)} indicators waiting past {minutes} minutes to be "
            "linked for intake. The daemon's logs show why indexing fails.",
            "link": LINK,
        }
    ]


def register() -> None:
    notices.CONDITIONS.extend([waiting_too_long])
