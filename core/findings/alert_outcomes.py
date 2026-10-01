"""One terminal state per finding that arrived on a UTC day.

The live states partition those arrivals, so their counts sum to the
inflow. #1319 does not call this: Triage speaks in intake states.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, Sequence

from core.findings.arrival_counts import utc_day_bounds
from core.response.approval_service import pending_approval_case_ids
from core.storage.connection import get_db_manager
from core.storage.models import Case, CaseClosureInfo, Finding, case_findings

TERMINAL_LABELS = {
    "needs_you": "Needs you",
    "resolved_auto": "Resolved automatically",
    "resolved_person": "Closed by a person",
    "working": "Still working",
    "waiting": "Not in a case",
}


@dataclass(frozen=True)
class _LinkedCase:
    case_id: str
    status: str
    updated_at: datetime
    closed_by_kind: Optional[str]


def _state(case: Optional[_LinkedCase], pending: set[str]) -> str:
    if case is None:
        return "waiting"
    if case.case_id in pending:
        return "needs_you"
    if case.status == "closed":
        # No closure row counts as an agent close: close_case defaults the kind.
        if case.closed_by_kind == "analyst":
            return "resolved_person"
        return "resolved_auto"
    return "working"


def _cases_for(session, finding_ids: Sequence[str]) -> dict[str, _LinkedCase]:
    if not finding_ids:
        return {}
    rows = (
        session.query(
            case_findings.c.finding_id,
            Case.case_id,
            Case.status,
            Case.updated_at,
            CaseClosureInfo.closed_by_kind,
        )
        .join(Case, Case.case_id == case_findings.c.case_id)
        .outerjoin(CaseClosureInfo, CaseClosureInfo.case_id == Case.case_id)
        .filter(case_findings.c.finding_id.in_(list(finding_ids)))
        .all()
    )
    best: dict[str, _LinkedCase] = {}
    for finding_id, case_id, status, updated_at, kind in rows:
        candidate = _LinkedCase(case_id, status, updated_at, kind)
        current = best.get(finding_id)
        if current is None or (updated_at, case_id) > (
            current.updated_at,
            current.case_id,
        ):
            best[finding_id] = candidate
    return best


def terminal_states_for(finding_ids: Sequence[str]) -> dict[str, str]:
    """The same partition ``terminal_states_today`` uses, for these findings."""
    ids = list(finding_ids)
    if not ids:
        return {}
    pending = pending_approval_case_ids()
    db = get_db_manager()
    with db.session_scope() as session:
        cases = _cases_for(session, ids)
    return {finding_id: _state(cases.get(finding_id), pending) for finding_id in ids}


def terminal_states_today(day: Optional[date] = None) -> list[dict]:
    """One row per finding created that day: id, source, and one state."""
    start, end = utc_day_bounds(day)
    db = get_db_manager()
    with db.session_scope() as session:
        rows = (
            session.query(Finding.finding_id, Finding.data_source)
            .filter(Finding.created_at >= start, Finding.created_at < end)
            .all()
        )
    states = terminal_states_for([finding_id for finding_id, _source in rows])
    return [
        {
            "finding_id": finding_id,
            "data_source": source,
            "terminal_state": states[finding_id],
        }
        for finding_id, source in rows
    ]
