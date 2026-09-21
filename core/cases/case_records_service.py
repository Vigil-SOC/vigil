"""Persistence for case sub-records: tasks, relationships, escalations.

Callers pass the request-scoped ``Session``; nothing here commits — the unit of
work does — and nothing raises ``HTTPException``. Absence is ``None`` so the
router owns the status code.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from core.storage.models import (
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

# A finding-run is live in these statuses. Hunts have no Case, so they never
# match a ``case_id`` filter and are not selected by the bulk kill either.
LIVE_INVESTIGATION_STATUSES = (
    "assigned",
    "executing",
    "waiting_approval",
    "review_submitted",
)
_RESET_KILL_REASON = "killed: case reset"

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


def count_live_investigations(session: Session, case_id: str) -> int:
    """Live Investigations on this Case. Hunts have no Case, so they are not in it."""
    return (
        session.query(Investigation)
        .filter(
            Investigation.case_id == case_id,
            Investigation.status.in_(LIVE_INVESTIGATION_STATUSES),
        )
        .count()
    )


def kill_live_case_investigations(session: Session) -> List[Investigation]:
    """Fail every live Case-bearing Investigation. Hunts are not selected.

    Same terminal as the kill path: ``status=failed`` and the workdir state
    says so. The Case rows still exist; the caller deletes them after, and
    the FK SET NULLs ``case_id`` on these rows as history.
    """
    live = (
        session.query(Investigation)
        .filter(
            Investigation.case_id.isnot(None),
            Investigation.status.in_(LIVE_INVESTIGATION_STATUSES),
        )
        .all()
    )
    killed: List[Investigation] = []
    for inv in live:
        if not inv.case_id:
            continue
        inv.status = "failed"
        inv.master_review_notes = _RESET_KILL_REASON
        _fail_workdir(inv, _RESET_KILL_REASON)
        killed.append(inv)
    session.flush()
    return killed


def _fail_workdir(inv: Investigation, reason: str) -> None:
    """Mirror the kill endpoint's workdir write; skip when there is no directory."""
    raw = (inv.workdir or "").strip()
    if not raw:
        return
    workdir = Path(raw)
    if not workdir.is_dir():
        return
    state_path = workdir / "state.json"
    state: Dict[str, Any] = {}
    if state_path.exists():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "Could not read workdir state for %s; writing failed over it: %s",
                inv.investigation_id,
                e,
            )
    state["status"] = "failed"
    state["failure_reason"] = reason
    try:
        state_path.write_text(
            json.dumps(state, indent=2, default=str), encoding="utf-8"
        )
    except OSError as e:
        logger.warning(
            "Could not write workdir state for %s: %s", inv.investigation_id, e
        )


def purge_all_cases(session: Session) -> int:
    """Delete every case and its derived records. Returns the case count removed.

    Live Case-bearing Investigations are killed first so a finding-run is
    never left running with ``case_id`` SET NULL. Completed rows keep their
    ``case_id`` until the Case delete; the FK SET NULLs them as history.
    Hunts (``case_id`` already null) are not touched.
    """
    count = session.query(Case).count()
    kill_live_case_investigations(session)

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
    return count
