"""A Case's status: the words it may hold, and the close or reopen an edit makes.

Every writer (the v1 API, the agent tools, the MCP tools) takes ``CaseStatus``
and records the transition through ``record_status_change``, so a status the
queue reads as open is never counted closed elsewhere.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional, get_args

from sqlalchemy.orm import Session

from core.cases.closure import ClosedByKind, ClosureCategory

CaseStatus = Literal["new", "open", "investigating", "closed"]
CASE_STATUSES: tuple[str, ...] = get_args(CaseStatus)
CLOSED = "closed"


class StatusChange(str, Enum):
    CLOSE = "close"
    REOPEN = "reopen"


def status_change(
    previous: Optional[str], new: Optional[CaseStatus]
) -> Optional[StatusChange]:
    """The close or reopen setting ``new`` over ``previous`` makes, if any.

    Re-sending ``closed`` onto a closed Case is an edit: closing it again would
    move the closure's date and re-derive its Verdict for nothing.
    """
    if new is None or (previous == CLOSED) == (new == CLOSED):
        return None
    return StatusChange.CLOSE if new == CLOSED else StatusChange.REOPEN


def record_status_change(
    session: Session,
    case_id: str,
    change: StatusChange,
    *,
    closed_by: str,
    closed_by_kind: ClosedByKind,
) -> None:
    """Record a status edit's close with no category stated, or its reopen.

    ``unspecified`` is not a determination, so ``close_case`` keeps one already
    on record. A caller with a determination calls ``close_case`` itself.
    """
    from core.cases.case_workflow_service import CaseWorkflowService

    workflow = CaseWorkflowService()
    if change is StatusChange.CLOSE:
        workflow.close_case(
            session,
            case_id,
            closure_category=ClosureCategory.UNSPECIFIED,
            closed_by=closed_by,
            closed_by_kind=closed_by_kind,
        )
    else:
        workflow.reopen_case(session, case_id)
