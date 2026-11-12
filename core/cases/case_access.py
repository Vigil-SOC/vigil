"""Restricted cases where the loaders don't reach (#4358).

The rule is ``core.auth.permissions.may_read_case``; this only asks it of one
case row, for the paths that read a case's children by id (MCP tools), tell the
chat brief why it was left out, and find a run's own case.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.auth.permissions import may_read_case
from core.auth.tool_principal import current_claims
from core.storage.models import Case, WorkflowRun
from core.storage.unit_of_work import unit_of_work

READ = "read"
WITHHELD = "withheld"
MISSING = "missing"


def case_access(case_id: str) -> str:
    """READ, WITHHELD (restricted and the caller is not a reader) or MISSING."""
    from core.storage.database_data_service import get_database_data_service

    # Demo mode holds no restricted cases, and its cases are not rows.
    if not get_database_data_service().is_using_database():
        return READ
    with unit_of_work() as session:
        case = session.get(Case, case_id)
        if case is None:
            return MISSING
        return READ if may_read_case(case) else WITHHELD


def readable(case_id: str) -> bool:
    """Whether the caller may read this case. Absent and refused are one answer."""
    return case_access(case_id) == READ


def run_case_id(session: Session, run_id: Optional[str]) -> Optional[str]:
    """The case a run was started on, if any. A chat turn has no run row."""
    if not run_id:
        return None
    return session.scalar(
        select(WorkflowRun.trigger_context["case_id"].astext).where(
            WorkflowRun.run_id == run_id
        )
    )


def lost_reader() -> Optional[str]:
    """Why a run acting for a person may go no further: its own case is now
    restricted and that person is not a reader. None otherwise. Asked of the run's
    own case only, so it says nothing about a case the run never held."""
    claims = current_claims()
    if claims is None or not claims.person_id or not claims.run_id:
        return None
    with unit_of_work() as session:
        case_id = run_case_id(session, claims.run_id)
        case = session.get(Case, case_id) if case_id else None
        if case is None or may_read_case(case):
            return None
    who = claims.username or claims.person_id
    return (
        f"case {case_id} is restricted and {who} is no longer one of its readers; "
        "the run waits until a reader adds them back"
    )
