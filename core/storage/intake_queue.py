"""The one insert of a queued intake row.

Callers in the daemon and the API share it, and so does a removal that sends
an alert back (#4850). A second queued row for the same finding is a no-op:
``uq_intake_triggers_queued_finding`` already says so.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from core.storage.connection import get_db_manager
from core.storage.models import IntakeTrigger
from core.time import utcnow

logger = logging.getLogger(__name__)

# Rank order for the intake queue. Alphabetically ``low`` precedes ``medium``,
# so nothing sorts on the severity string.
SEVERITY_BANDS = ("critical", "high", "medium", "low", "unknown")
_BANDS = frozenset(SEVERITY_BANDS)


def intake_severity_band(
    kind: Optional[str],
    *,
    finding_severity: Optional[str] = None,
    priority: Optional[str] = None,
) -> str:
    """Map a row onto a ranked band. Detection reads the finding; others the row."""
    raw = finding_severity if kind == "detection" else priority
    if raw is None or not str(raw).strip():
        return "unknown"
    name = str(raw).strip().lower()
    return name if name in _BANDS else "unknown"


def insert_intake_trigger(
    *,
    kind: str,
    priority: str = "medium",
    finding_id: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    session: Optional[Session] = None,
) -> Optional[int]:
    """Insert a queued trigger. None when a queued row for this finding already exists.

    ``session`` joins the caller's transaction and does not commit it. Without
    one, this opens and commits its own.
    """
    stmt = (
        pg_insert(IntakeTrigger)
        .values(
            kind=kind,
            state="queued",
            finding_id=finding_id,
            priority=priority or "medium",
            payload=payload or {},
            created_at=utcnow(),
        )
        .on_conflict_do_nothing(
            index_elements=[IntakeTrigger.finding_id],
            index_where=text("state = 'queued' AND finding_id IS NOT NULL"),
        )
        .returning(IntakeTrigger.id)
    )

    def _run(db: Session) -> Optional[int]:
        new_id = db.execute(stmt).scalar_one_or_none()
        if new_id is None and finding_id:
            logger.info("intake already has a queued row for finding %s", finding_id)
        return new_id

    if session is None:
        with get_db_manager().session_scope() as owned:
            return _run(owned)
    # A refused insert rolls back to here, so the caller's transaction stays usable.
    with session.begin_nested():
        return _run(session)
