"""The mark on a re-decision run, and the close time it stores.

Nothing here imports the starter. The run listing, recall, and the search
ceiling all read this module, and the starter imports them back.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from sqlalchemy import select

from core.storage.models.workflow import WorkflowRun
from core.storage.unit_of_work import unit_of_work

MARK = "re_decision"
WORKFLOW_ID = "re-decision"
NOT_STARTED_HERE = "this workflow is not started here"

_MISSING = object()
_CLOSES: Dict[str, Optional[datetime]] = {}


def as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def marked(trigger_context: Optional[Mapping[str, Any]]) -> bool:
    """True when this run is a re-decision. A live run's context is not."""
    return trigger_context is not None and trigger_context.get(MARK) is True


def _parse_close(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return as_utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return as_utc(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
    except ValueError:
        return None


def redecision_close(run_id: Optional[str]) -> Optional[datetime]:
    """The close time on a marked run, else None. Live runs pass through."""
    if not run_id:
        return None
    cached = _CLOSES.get(run_id, _MISSING)
    if cached is not _MISSING:
        return cached  # type: ignore[return-value]
    close = _read_close(run_id)
    if len(_CLOSES) > 256:
        _CLOSES.clear()
    _CLOSES[run_id] = close
    return close


def remember_close(run_id: str, close: datetime) -> None:
    """The starter records the close it just wrote, so the next read skips the row."""
    if len(_CLOSES) > 256:
        _CLOSES.clear()
    _CLOSES[run_id] = close


def _read_close(run_id: str) -> Optional[datetime]:
    with unit_of_work() as session:
        context = session.scalar(
            select(WorkflowRun.trigger_context).where(WorkflowRun.run_id == run_id)
        )
    if not isinstance(context, dict) or not marked(context):
        return None
    return _parse_close(context.get("closed_at"))
