"""Persistence for case sub-records: tasks, relationships, escalations.

Callers pass the request-scoped ``Session``; nothing here commits — the unit of
work does — and nothing raises ``HTTPException``. Absence is ``None`` so the
router owns the status code.
"""

import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from core.auth.permissions import case_read_clause, may_read_case
from core.cases import case_trail
from core.storage.models import (
    LIVE_INVESTIGATION_STATUSES,
    RUN_LIVE_STATE,
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
    Finding,
    IntakeTrigger,
    Investigation,
    SketchMapping,
    WorkflowRun,
    WorkflowRunPhase,
    case_findings,
)

logger = logging.getLogger(__name__)

RESET_KILL_REASON = "killed: case reset"


class PurgeResult(NamedTuple):
    cases: int
    killed_investigation_ids: List[str]
    # Restricted cases the caller may not read: left in place with their records.
    kept: int = 0


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


def list_case_investigations(session: Session, case_id: str) -> List[Investigation]:
    """Newest first. The audit run is ``run_id_for`` of the first row."""
    return (
        session.query(Investigation)
        .filter(Investigation.case_id == case_id)
        .order_by(
            Investigation.created_at.desc(), Investigation.investigation_id.desc()
        )
        .all()
    )


def run_liveness(session: Session, case_ids: Iterable[str]) -> Dict[str, bool]:
    """``{case_id: whether a run on it is live}``; a case no run ever touched is absent."""
    ids = list(case_ids)
    live: Dict[str, bool] = {}
    if not ids:
        return live
    for case_id, status in session.query(
        Investigation.case_id, Investigation.status
    ).filter(Investigation.case_id.in_(ids)):
        live[case_id] = live.get(case_id, False) or (
            status in LIVE_INVESTIGATION_STATUSES
        )
    run_case = WorkflowRun.trigger_context["case_id"].astext
    for case_id, status in session.query(run_case, WorkflowRun.status).filter(
        run_case.in_(ids), WorkflowRun.deleted_at.is_(None)
    ):
        live[case_id] = live.get(case_id, False) or status in RUN_LIVE_STATE
    return live


def alerts_since_last_run(session: Session, case_id: str) -> int:
    """Alerts merged after a case's newest run started, so that run did not read them.

    Read from the intake ledger rather than kept as a count, so a restart or a merge
    during a live run needs no bookkeeping. A case with no run has no last run and
    reads 0. Noise-marked alerts do not count. An alert merged twice counts once. A
    person-started run has a ``WorkflowRun`` and no ``Investigation``, and it counts
    as the case's last run all the same.
    """
    starts = [
        session.query(func.max(Investigation.created_at))
        .filter(Investigation.case_id == case_id)
        .scalar(),
        session.query(func.max(WorkflowRun.started_at))
        .filter(
            WorkflowRun.trigger_context["case_id"].astext == case_id,
            WorkflowRun.deleted_at.is_(None),
        )
        .scalar(),
    ]
    starts = [at for at in starts if at is not None]
    if not starts:
        return 0
    return (
        session.query(func.count(distinct(IntakeTrigger.finding_id)))
        .join(Finding, Finding.finding_id == IntakeTrigger.finding_id)
        .join(case_findings, case_findings.c.finding_id == IntakeTrigger.finding_id)
        .filter(
            case_findings.c.case_id == case_id,
            IntakeTrigger.merged_into == case_id,
            IntakeTrigger.state == "merged",
            IntakeTrigger.decided_at > max(starts),
            Finding.noise_marked_at.is_(None),
        )
        .scalar()
    )


def list_case_runs(session: Session, case_id: str) -> List[WorkflowRun]:
    """Runs started on this case, newest first, found by ``trigger_context``.

    ``/hunt`` and a bridge handoff start a run with the case id in its trigger
    context and no ``Investigation`` row.
    """
    return (
        session.query(WorkflowRun)
        .filter(
            WorkflowRun.trigger_context["case_id"].astext == case_id,
            WorkflowRun.deleted_at.is_(None),
        )
        .order_by(WorkflowRun.started_at.desc(), WorkflowRun.run_id.desc())
        .all()
    )


def run_step_counts(session: Session, run_ids: Sequence[str]) -> Dict[str, int]:
    """Phases recorded per run, in one query. A run with none is absent."""
    if not run_ids:
        return {}
    rows = (
        session.query(WorkflowRunPhase.run_id, func.count())
        .filter(WorkflowRunPhase.run_id.in_(run_ids))
        .group_by(WorkflowRunPhase.run_id)
        .all()
    )
    return {run_id: int(count) for run_id, count in rows}


def list_case_audit(session: Session, case_id: str) -> List[CaseAuditLog]:
    """Rows about this case. There is no ``case_id`` column on the table.

    None once the table is gone: python -m core.upgrade drops it (#3310).
    """
    from core.storage.connection import table_present

    if not table_present(session, "case_audit_logs"):
        return []
    return (
        session.query(CaseAuditLog)
        .filter(
            CaseAuditLog.entity_type == "case",
            CaseAuditLog.entity_id == case_id,
        )
        .all()
    )


def list_case_trail(session: Optional[Session], case_id: str) -> List[dict]:
    """This case's audit trail entries, oldest first; none when it is not in use."""
    from core.audit import trail

    if session is None or not trail.in_use(session):
        return []
    return trail.read_all(session, trail.make_query(object_ref=case_id))


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
    signed_in: Optional[str],
) -> CaseTask:
    """Add a task and its trail entry. ``signed_in`` is who added it."""
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
    _task_entry(
        session, case_trail.TASK, task, signed_in, description, owner=(None, assignee)
    )
    return task


def _task_entry(
    session: Session,
    action: str,
    task: CaseTask,
    signed_in: Optional[str],
    description: Optional[str],
    owner: Optional[tuple[Optional[str], Optional[str]]] = None,
) -> None:
    # Ids, the status and the holders are hashed; the title, description and
    # the holders' stored text are free text. ``owner`` is (previous, new) when
    # the assignee changed; a new task with none records none.
    held = owner is not None and any(owner)
    texts = [task.title, description, *(case_trail.holder_text(*owner) if held else [])]
    case_trail.append_or_legacy(
        session,
        action=action,
        signed_in=signed_in,
        case_id=task.case_id,
        operator_text="\n\n".join(t for t in texts if t) or None,
        ids=lambda: {
            "task_id": task.task_id,
            "status": task.status,
            **(case_trail.holders(session, *owner) if held else {}),
        },
    )


def tasks_on_case(session: Session, case_id: str) -> List[CaseTask]:
    return session.query(CaseTask).filter(CaseTask.case_id == case_id).all()


def list_tasks(case_id: str) -> List[CaseTask]:
    """Tasks for a case, or [] when the database is unreachable.

    Runs in its own transaction: a failed query must not poison a
    request-scoped session, which would turn this fallback into a 500.
    """
    from core.storage.unit_of_work import unit_of_work

    try:
        with unit_of_work() as session:
            return tasks_on_case(session, case_id)
    except Exception:
        logger.exception("Listing tasks for case %s failed; reporting none", case_id)
        return []


def update_task(
    session: Session,
    task_id: int,
    updates: Dict[str, Any],
    *,
    signed_in: Optional[str],
) -> Optional[CaseTask]:
    """Apply non-None ``updates`` to a task. Returns None if it doesn't exist.

    Driven off the payload rather than a field list held here: TaskUpdate is
    already the definition of what a caller may change, and a second copy of
    it only stays right until someone adds a field. An ``assignee`` goes
    through ``resolve_assignee``; blank unassigns, None leaves it. A refusal
    raises ``AssigneeRefused`` before anything is written.
    """
    from core.cases.assignee import require_owner_reads, resolve_assignee

    task = session.query(CaseTask).filter(CaseTask.task_id == task_id).first()
    if task is None:
        return None
    updates = dict(updates)
    owner = None
    raw = updates.pop("assignee", None)
    if raw is not None:
        previous = task.assignee
        task.assignee = resolve_assignee(session, raw, current=previous)
        if task.assignee != previous:
            owner = (previous, task.assignee)
            # A newly named assignee must read a restricted case; an echo saves.
            require_owner_reads(session, session.get(Case, task.case_id), task.assignee)
    for field, value in updates.items():
        if value is not None and hasattr(task, field):
            setattr(task, field, value)
    session.flush()
    _task_entry(
        session,
        case_trail.TASK_UPDATE,
        task,
        signed_in,
        updates.get("description"),
        owner=owner,
    )
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
    """One link and its entry on this case's trail; ``created_by`` is the actor."""
    rel = CaseRelationship(
        case_id=case_id,
        related_case_id=related_case_id,
        relationship_type=relationship_type,
        created_by=created_by,
        notes=notes,
    )
    session.add(rel)
    session.flush()
    case_trail.append_or_legacy(
        session,
        action=case_trail.LINK,
        signed_in=created_by,
        case_id=case_id,
        operator_text=notes,
        ids=lambda: {
            "relationship_id": rel.relationship_id,
            "related_case_id": related_case_id,
            "relationship_type": relationship_type,
        },
    )
    return rel


def link_cases(
    session: Session,
    case_a: str,
    case_b: str,
    *,
    relationship_type: str,
    created_by: str,
    notes: Optional[str] = None,
) -> None:
    """Link two cases both ways, skipping a direction that already has the link.

    A case's relationships are read by its own case_id only, so one row would
    show the link on one side. Each row added is an entry on its own case.
    """
    for case_id, related in ((case_a, case_b), (case_b, case_a)):
        exists = (
            session.query(CaseRelationship.relationship_id)
            .filter_by(
                case_id=case_id,
                related_case_id=related,
                relationship_type=relationship_type,
            )
            .first()
        )
        if exists is None:
            add_relationship(
                session,
                case_id,
                related_case_id=related,
                relationship_type=relationship_type,
                created_by=created_by,
                notes=notes,
            )


def list_relationships(
    session: Session, case_id: str
) -> List[Tuple[CaseRelationship, Optional[str], Optional[str]]]:
    """Each link with its related Case's title and status, in one query.

    The inner join drops a row whose related Case is gone; both FKs cascade,
    so in Postgres there is none to drop.
    """
    return (
        session.query(CaseRelationship, Case.title, Case.status)
        .join(Case, Case.case_id == CaseRelationship.related_case_id)
        .filter(CaseRelationship.case_id == case_id)
        .order_by(CaseRelationship.relationship_id)
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
    # A restricted case the caller may not read is not there (#4354).
    if case is None or not may_read_case(case):
        return None

    live = live_investigation_ids(session, case_id)
    if live:
        return live

    session.delete(case)
    session.flush()
    case_trail.append_case(
        session, case_trail.DELETE, case_id, operator_text=case.title or None
    )
    return []


def kill_live_case_investigations(
    session: Session, case_ids: Any = None
) -> List[Investigation]:
    """Fail every live Case-bearing Investigation. Hunts are not selected.

    ``status=failed`` drops the row out of the supervision loop, which
    reconciles only executing and waiting_approval rows, so nothing puts it
    back. The workdir sidecar the kill endpoint also writes is the caller's:
    ``core`` cannot import the daemon that owns it.
    """
    query = session.query(Investigation).filter(
        Investigation.case_id.isnot(None),
        Investigation.status.in_(LIVE_INVESTIGATION_STATUSES),
    )
    if case_ids is not None:
        query = query.filter(Investigation.case_id.in_(case_ids))
    live = query.all()
    for inv in live:
        inv.status = "failed"
        inv.master_review_notes = RESET_KILL_REASON
    session.flush()
    return live


def purge_all_cases(session: Session) -> PurgeResult:
    """Delete every case the caller may read and its derived records, killing
    live runs first.

    Live Case-bearing Investigations are failed before the delete so a
    finding-run is never left running with ``case_id`` SET NULL. Completed rows
    keep their ``case_id`` until the Case delete; the FK SET NULLs them as
    history. Hunts (``case_id`` already null) are not touched. A restricted case
    the caller may not read is kept, with every record of its own, and counted.
    """
    readable = case_read_clause()
    # Every delete is scoped to these ids, so a case written after this read
    # (restricted or not) is never swept up with them.
    ids = [row[0] for row in session.execute(select(Case.case_id).where(readable))]
    kept = session.query(Case).filter(~readable).count()
    killed = [
        inv.investigation_id for inv in kill_live_case_investigations(session, ids)
    ]

    for model in _CASE_OWNED_MODELS:
        session.query(model).filter(model.case_id.in_(ids)).delete(
            synchronize_session=False
        )
    session.execute(case_findings.delete().where(case_findings.c.case_id.in_(ids)))
    for model in (SketchMapping, AIDecisionLog):
        session.query(model).filter(model.case_id.in_(ids)).delete(
            synchronize_session=False
        )
    from core.storage.connection import table_present

    if table_present(session, "case_audit_logs"):
        # The legacy store names a case only as an entity id: wholesale, as
        # before, unless a kept case's rows must stay.
        legacy = session.query(CaseAuditLog)
        if kept:
            legacy = legacy.filter(CaseAuditLog.entity_id.in_(ids))
        legacy.delete(synchronize_session=False)
    session.query(Case).filter(Case.case_id.in_(ids)).delete(synchronize_session=False)
    # One entry for the purge, with the count; not one per case.
    case_trail.append_case(
        session,
        case_trail.PURGE,
        None,
        count=len(ids),
        killed_investigations=len(killed),
        kept=kept or None,
    )
    return PurgeResult(cases=len(ids), killed_investigation_ids=killed, kept=kept)
