"""Persistence for case sub-records: tasks, relationships, escalations.

Callers pass the request-scoped ``Session``; nothing here commits — the unit of
work does — and nothing raises ``HTTPException``. Absence is ``None`` so the
router owns the status code.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, NamedTuple, Optional

from sqlalchemy.orm import Session

from core.storage.models import (
    LIVE_INVESTIGATION_STATUSES,
    AIDecisionLog,
    Case,
    CaseAttachment,
    CaseAuditLog,
    CaseClosureInfo,
    CaseComment,
    CaseEscalation,
    CaseEvidence,
    CaseIOC,
    CaseMetrics,
    CaseNotification,
    CaseRelationship,
    CaseSLA,
    CaseTask,
    CaseWatcher,
    Investigation,
    SketchMapping,
    case_findings,
)

logger = logging.getLogger(__name__)

RESET_KILL_REASON = "killed: case reset"


class PurgeResult(NamedTuple):
    cases: int
    killed_investigation_ids: List[str]


# Purged wholesale by :func:`purge_all_cases`; order matters only in that
# child rows go before ``Case`` itself.
_CASE_OWNED_MODELS = (
    CaseAttachment,
    CaseClosureInfo,
    CaseEscalation,
    CaseEvidence,
    CaseIOC,
    CaseMetrics,
    CaseNotification,
    CaseRelationship,
    CaseSLA,
    CaseTask,
    CaseWatcher,
    CaseComment,
)


def add_task(
    session: Session,
    case_id: str,
    *,
    title: str,
    description: Optional[str],
    assignee: Optional[str],
    priority: str,
    due_date: Optional[datetime],
    checklist_items: Optional[List[Dict]],
) -> CaseTask:
    task = CaseTask(
        case_id=case_id,
        title=title,
        description=description,
        assignee=assignee,
        priority=priority,
        status="pending",
        due_date=due_date,
        checklist_items=checklist_items or [],
    )
    session.add(task)
    session.flush()
    return task


def list_tasks(case_id: str) -> List[CaseTask]:
    """Tasks for a case, or [] when the database is unreachable.

    Runs in its own transaction: a failed query must not poison a
    request-scoped session, which would turn this fallback into a 500.
    """
    from core.storage.unit_of_work import unit_of_work

    try:
        with unit_of_work() as session:
            return session.query(CaseTask).filter(CaseTask.case_id == case_id).all()
    except Exception:
        logger.exception("Listing tasks for case %s failed; reporting none", case_id)
        return []


def update_task(
    session: Session, task_id: int, updates: Dict[str, Any]
) -> Optional[CaseTask]:
    """Apply non-None ``updates`` to a task. Returns None if it doesn't exist.

    Driven off the payload rather than a field list held here: TaskUpdate is
    already the definition of what a caller may change, and a second copy of
    it only stays right until someone adds a field.
    """
    task = session.query(CaseTask).filter(CaseTask.task_id == task_id).first()
    if task is None:
        return None
    for field, value in updates.items():
        if value is not None and hasattr(task, field):
            setattr(task, field, value)
    session.flush()
    return task


def add_relationship(
    session: Session,
    case_id: str,
    *,
    related_case_id: str,
    relationship_type: str,
    created_by: str,
    notes: Optional[str] = None,
) -> CaseRelationship:
    rel = CaseRelationship(
        case_id=case_id,
        related_case_id=related_case_id,
        relationship_type=relationship_type,
        created_by=created_by,
        notes=notes,
    )
    session.add(rel)
    session.flush()
    return rel


def list_relationships(session: Session, case_id: str) -> List[CaseRelationship]:
    return (
        session.query(CaseRelationship)
        .filter(CaseRelationship.case_id == case_id)
        .all()
    )


def list_escalations(session: Session, case_id: str) -> List[CaseEscalation]:
    return session.query(CaseEscalation).filter(CaseEscalation.case_id == case_id).all()


def live_investigation_ids(session: Session, case_id: str) -> List[str]:
    """Live Investigations on this Case. Hunts have no Case, so they are not in it."""
    rows = (
        session.query(Investigation.investigation_id)
        .filter(
            Investigation.case_id == case_id,
            Investigation.status.in_(LIVE_INVESTIGATION_STATUSES),
        )
        .all()
    )
    return [row[0] for row in rows]


def delete_case(session: Session, case_id: str) -> Optional[List[str]]:
    """Delete a Case that has no live Investigation, in the caller's transaction.

    Returns ``None`` when there is no such Case, the blocking Investigation ids
    when there are any, and an empty list once the Case is deleted. The check
    and the delete share a transaction so a run claimed in between cannot have
    its ``case_id`` SET NULL out from under it.
    """
    case = session.get(Case, case_id)
    if case is None:
        return None

    live = live_investigation_ids(session, case_id)
    if live:
        return live

    session.delete(case)
    session.flush()
    return []


def kill_live_case_investigations(session: Session) -> List[Investigation]:
    """Fail every live Case-bearing Investigation. Hunts are not selected.

    ``status=failed`` drops the row out of the supervision loop, which
    reconciles only executing and waiting_approval rows, so nothing puts it
    back. The workdir sidecar the kill endpoint also writes is the caller's:
    ``core`` cannot import the daemon that owns it.
    """
    live = (
        session.query(Investigation)
        .filter(
            Investigation.case_id.isnot(None),
            Investigation.status.in_(LIVE_INVESTIGATION_STATUSES),
        )
        .all()
    )
    for inv in live:
        inv.status = "failed"
        inv.master_review_notes = RESET_KILL_REASON
    session.flush()
    return live


def purge_all_cases(session: Session) -> PurgeResult:
    """Delete every case and its derived records, killing live runs first.

    Live Case-bearing Investigations are failed before the delete so a
    finding-run is never left running with ``case_id`` SET NULL. Completed rows
    keep their ``case_id`` until the Case delete; the FK SET NULLs them as
    history. Hunts (``case_id`` already null) are not touched.
    """
    count = session.query(Case).count()
    killed = [inv.investigation_id for inv in kill_live_case_investigations(session)]

    for model in _CASE_OWNED_MODELS:
        session.query(model).delete(synchronize_session=False)

    session.execute(case_findings.delete())
    session.query(SketchMapping).filter(SketchMapping.case_id.isnot(None)).delete(
        synchronize_session=False
    )
    session.query(AIDecisionLog).filter(AIDecisionLog.case_id.isnot(None)).delete(
        synchronize_session=False
    )
    session.query(CaseAuditLog).delete(synchronize_session=False)
    session.query(Case).delete(synchronize_session=False)
    return PurgeResult(cases=count, killed_investigation_ids=killed)
