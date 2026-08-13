"""Persistence for case sub-records: tasks, relationships, escalations.

Callers pass the request-scoped ``Session``; nothing here commits — the unit of
work does — and nothing raises ``HTTPException``. Absence is ``None`` so the
router owns the status code.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from core.storage.models import (
    AIDecisionLog,
    AttackLayer,
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

_TASK_FIELDS = (
    "title",
    "description",
    "status",
    "assignee",
    "priority",
    "due_date",
    "completed_at",
    "actual_hours",
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
    """Apply non-None ``updates`` to a task. Returns None if it doesn't exist."""
    task = session.query(CaseTask).filter(CaseTask.task_id == task_id).first()
    if task is None:
        return None
    for field in _TASK_FIELDS:
        value = updates.get(field)
        if value is not None:
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


def purge_all_cases(session: Session) -> int:
    """Delete every case and its derived records. Returns the case count removed.

    Rows in other tables that merely *reference* a case (investigations, attack
    layers) are detached rather than deleted — they outlive the case.
    """
    count = session.query(Case).count()

    for model in _CASE_OWNED_MODELS:
        session.query(model).delete(synchronize_session=False)

    session.execute(case_findings.delete())
    session.query(SketchMapping).filter(SketchMapping.case_id.isnot(None)).delete(
        synchronize_session=False
    )
    session.query(AIDecisionLog).filter(AIDecisionLog.case_id.isnot(None)).delete(
        synchronize_session=False
    )
    session.query(Investigation).filter(Investigation.case_id.isnot(None)).update(
        {"case_id": None}, synchronize_session=False
    )
    session.query(AttackLayer).filter(AttackLayer.case_id.isnot(None)).update(
        {"case_id": None}, synchronize_session=False
    )
    session.query(CaseAuditLog).delete(synchronize_session=False)
    session.query(Case).delete(synchronize_session=False)
    return count
