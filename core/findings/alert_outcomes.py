"""One terminal state per finding that arrived on a UTC day.

The live states partition those arrivals, so their counts sum to the
inflow. #1319 does not call this: Triage speaks in intake states.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, Sequence

from core.cases.closure import closed_by_person
from core.findings.arrival_counts import utc_day_bounds
from core.response.approval_service import needs_you_case_ids
from core.storage.connection import get_db_manager
from core.storage.models import (
    Case,
    CaseClosureInfo,
    Finding,
    IntakeTrigger,
    case_findings,
)

TERMINAL_LABELS = {
    "dismissed": "Dropped",
    "needs_you": "Needs you",
    "resolved_auto": "Resolved automatically",
    "resolved_person": "Closed by a person",
    "working": "Still working",
    "scoring": "Waiting for triage",
    "waiting": "Not picked up",
}


@dataclass(frozen=True)
class _LinkedCase:
    case_id: str
    status: str
    updated_at: datetime
    close_channel: Optional[str]


def _state(
    case: Optional[_LinkedCase],
    needs_you_ids: set[str],
    *,
    dismissed: bool = False,
    scoring: bool = False,
) -> str:
    # A dismissal is the finding's own terminal state, ahead of any case.
    if dismissed:
        return "dismissed"
    if case is None:
        return "scoring" if scoring else "waiting"
    if case.case_id in needs_you_ids:
        return "needs_you"
    if case.status == "closed":
        if closed_by_person(case.close_channel):
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
            CaseClosureInfo.close_channel,
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


def stored_finding_states(data, finding_ids: Sequence[str]) -> dict:
    """``finding_states`` for findings ``data`` read, or {} when it reads no database (demo mode)."""
    info = getattr(data, "get_backend_info", None)
    if info is None or not info().get("database_available"):
        return {}
    return finding_states(finding_ids)


# An all-time read can hold every finding; one IN list per this many ids.
_ID_CHUNK = 5000


def finding_states(finding_ids: Sequence[str]) -> dict[str, tuple[str, Optional[str]]]:
    """``{finding_id: (terminal_state, case_id)}``; the case is the one the state reads."""
    ids = list(finding_ids)
    if not ids:
        return {}
    needs_you_ids = needs_you_case_ids()
    cases: dict[str, _LinkedCase] = {}
    dismissed: set[str] = set()
    scoring: set[str] = set()
    db = get_db_manager()
    with db.session_scope() as session:
        for at in range(0, len(ids), _ID_CHUNK):
            chunk = ids[at : at + _ID_CHUNK]
            cases.update(_cases_for(session, chunk))
            dismissed.update(
                row[0]
                for row in session.query(Finding.finding_id).filter(
                    Finding.finding_id.in_(chunk), Finding.noise_marked_at.isnot(None)
                )
            )
            scoring.update(
                row[0]
                for row in session.query(IntakeTrigger.finding_id).filter(
                    IntakeTrigger.finding_id.in_(chunk),
                    IntakeTrigger.state == "queued",
                )
            )
    return {
        finding_id: (
            _state(
                cases.get(finding_id),
                needs_you_ids,
                dismissed=finding_id in dismissed,
                scoring=finding_id in scoring,
            ),
            cases[finding_id].case_id if finding_id in cases else None,
        )
        for finding_id in ids
    }


def terminal_states_today(
    day: Optional[date] = None, *, all_time: bool = False
) -> list[dict]:
    """One row per finding created that day, or ever with ``all_time``: id, source, one state, and the case it reads."""
    db = get_db_manager()
    with db.session_scope() as session:
        query = session.query(Finding.finding_id, Finding.data_source)
        if not all_time:
            start, end = utc_day_bounds(day)
            query = query.filter(Finding.created_at >= start, Finding.created_at < end)
        rows = query.all()
    states = finding_states([finding_id for finding_id, _source in rows])
    return [
        {
            "finding_id": finding_id,
            "data_source": source,
            "terminal_state": states[finding_id][0],
            "case_id": states[finding_id][1],
        }
        for finding_id, source in rows
    ]
