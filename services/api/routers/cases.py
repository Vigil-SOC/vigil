"""Cases API endpoints."""

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from pydantic import BaseModel, Field, field_validator

from core.agents.projections import read_events
from core.auth.auth_service import AuthService
from core.auth.permissions import permission_gate
from core.cases import case_journal_service, case_records_service
from core.cases.case_attachment_service import attach_document
from core.cases.case_collaboration_service import CaseCollaborationService
from core.cases.case_evidence_service import CaseEvidenceService
from core.cases.case_notification_service import WATCHER_NOTIFICATION_TYPES
from core.cases.case_record import merge_record, unread_row
from core.cases.case_sla_service import CaseSLAService, SlaOutcome
from core.cases.case_state import case_run_refs, primary_ref
from core.cases.case_workflow_service import CaseWorkflowService
from core.documents.extract import ACCEPTED_TYPES, DocumentRefused, spool_upload
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.config_service import get_config_service
from core.storage.database_data_service import DatabaseDataService
from core.storage.models import Case, User
from core.storage.schemas import (
    CaseCommentSchema,
    CaseEscalationSchema,
    CaseEvidenceSchema,
    CaseRelationshipSchema,
    CaseSchema,
    CaseSLASchema,
    CaseSLAStatusSchema,
    CaseTaskSchema,
    CaseWatcherSchema,
)
from core.storage.schemas.case_api import (
    CaseCommentsResponse,
    CaseEscalationsResponse,
    CasePurgeResponse,
    CaseRecordResponse,
    CaseRelationshipsResponse,
    CaseSuccessResponse,
    CaseTasksResponse,
    CaseWatchersResponse,
)
from services.api.middleware.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()

_CASES_WRITE = [permission_gate("cases.write")]

ROUTER_META = RouterMeta(
    prefix="/api/cases",
    tags=["cases"],
    auth=Auth.REQUIRED,
)
data_service = DatabaseDataService()


class ActivityAdd(BaseModel):
    """Add activity to case."""

    activity_type: str  # e.g., "note", "status_change", "finding_added", "action_taken"
    description: str
    details: Optional[Dict[str, Any]] = None


class ResolutionStepAdd(BaseModel):
    """Add resolution step to case."""

    description: str
    action_taken: str
    result: Optional[str] = None


def _mark_workdirs_failed(investigation_ids: List[str], reason: str) -> None:
    """Mirror the kill endpoint's sidecar write for runs the reset failed.

    Runs after the request commits, because the sidecar cannot be rolled back
    with the rows. One unwritable workdir must not abandon the rest.
    """
    from core.config import get_settings
    from services.daemon.workdir import WorkdirManager

    workdir = WorkdirManager(get_settings().orchestrator_workdir)
    for investigation_id in investigation_ids:
        if not workdir.exists(investigation_id):
            continue
        try:
            state = workdir.read_state(investigation_id)
            state["status"] = "failed"
            state["failure_reason"] = reason
            workdir.write_state(investigation_id, state)
        except OSError as e:
            logger.warning(
                "Could not write workdir state for %s: %s", investigation_id, e
            )


@router.delete("/all", dependencies=_CASES_WRITE, response_model=CasePurgeResponse)
async def clear_all_cases(
    session: UnitOfWorkSession,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
):
    """Delete all cases and case-derived generated data (requires cases.delete)."""
    if not AuthService.check_permission(current_user.user_id, "cases.delete"):
        raise HTTPException(
            status_code=403, detail="Permission denied: cases.delete required"
        )

    result = case_records_service.purge_all_cases(session)
    killed = len(result.killed_investigation_ids)
    # Counts come back from the purge. record_audit commits on its own
    # session, so this row is durable before the request commits.
    get_config_service(user_id=str(current_user.user_id)).record_audit(
        config_type="cases",
        config_key="cases",
        action="delete_all",
        old_value={"count": result.cases, "killed_investigations": killed},
        new_value=None,
        change_reason="All cases deleted via API",
    )
    background_tasks.add_task(
        _mark_workdirs_failed,
        result.killed_investigation_ids,
        case_records_service.RESET_KILL_REASON,
    )

    return {
        "success": True,
        "deleted": result.cases,
        "killed_investigations": killed,
        "message": (
            f"Deleted {result.cases} cases and case-derived records; "
            f"killed {killed} live investigations"
        ),
    }


@router.post(
    "/{case_id}/activities", dependencies=_CASES_WRITE, response_model=CaseSchema
)
async def add_case_activity(case_id: str, activity: ActivityAdd):
    """
    Add an activity/action to a case.

    Args:
        case_id: The case ID
        activity: Activity data

    Returns:
        Updated case
    """
    if not data_service.get_case(case_id):
        raise HTTPException(status_code=404, detail="Case not found")

    added = case_journal_service.append_activity(
        data_service,
        case_id,
        activity_type=activity.activity_type,
        description=activity.description,
        details=activity.details,
    )
    if added is None:
        raise HTTPException(status_code=500, detail="Failed to add activity")

    return data_service.get_case(case_id)


@router.post(
    "/{case_id}/resolution-steps", dependencies=_CASES_WRITE, response_model=CaseSchema
)
async def add_resolution_step(case_id: str, step: ResolutionStepAdd):
    """
    Add a resolution step to a case.

    Args:
        case_id: The case ID
        step: Resolution step data

    Returns:
        Updated case
    """
    if not data_service.get_case(case_id):
        raise HTTPException(status_code=404, detail="Case not found")

    added = case_journal_service.append_resolution_step(
        data_service,
        case_id,
        description=step.description,
        action_taken=step.action_taken,
        result=step.result,
    )
    if added is None:
        raise HTTPException(status_code=500, detail="Failed to add resolution step")

    return data_service.get_case(case_id)


@router.delete(
    "/{case_id}",
    response_model=CaseSuccessResponse,
    dependencies=[permission_gate("cases.delete")],
)
async def delete_case(
    case_id: str,
    session: UnitOfWorkSession,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Delete a case that has no live Investigation (#1001)."""
    # Demo cases live in memory, not in the session, and nothing investigates
    # them, so there is no live run to guard against.
    if data_service.is_demo_mode():
        if not data_service.delete_case(case_id):
            raise HTTPException(status_code=404, detail="Case not found")
        return {"success": True}

    case = session.get(Case, case_id)
    title = case.title if case is not None else None
    live = case_records_service.delete_case(session, case_id)

    if live is None:
        raise HTTPException(status_code=404, detail="Case not found")
    if live:
        plural = "s" if len(live) != 1 else ""
        raise HTTPException(
            status_code=409,
            detail=(
                f"case has {len(live)} live investigation{plural} "
                f"({', '.join(live)}); kill or finish them first"
            ),
        )

    get_config_service(user_id=str(current_user.user_id)).record_audit(
        config_type="cases",
        config_key=case_id,
        action="delete",
        old_value={"case_id": case_id, "title": title},
        new_value=None,
        change_reason="Case deleted via API",
    )
    return {"success": True}


# =============================================================================
# Enhanced Case Management Endpoints
# =============================================================================


# SLA Management
class SLAAssign(BaseModel):
    """Assign SLA to case."""

    sla_policy_id: Optional[str] = None


# Why an assignment was refused, in the caller's terms. A 500 said "something
# went wrong here", which was never true of any of these: each one is a thing
# the operator named and can act on.
_SLA_REFUSALS = {
    SlaOutcome.NO_SUCH_CASE: (404, "Case {case_id} not found"),
    SlaOutcome.POLICY_NOT_FOUND: (404, "SLA policy {policy_id} not found"),
    SlaOutcome.POLICY_RETIRED: (
        409,
        "SLA policy {policy_id} is deactivated, so no new case takes it. "
        "Name an active policy, or reactivate this one with "
        "PUT /api/sla-policies/{policy_id} (is_active=true).",
    ),
    SlaOutcome.NO_DEFAULT_POLICY: (
        409,
        "No active default SLA policy exists for this case's priority.",
    ),
}


@router.post("/{case_id}/sla", dependencies=_CASES_WRITE, response_model=CaseSLASchema)
async def assign_sla(case_id: str, data: SLAAssign):
    """Assign SLA policy to case."""
    sla_service = CaseSLAService()
    assignment = sla_service.assign_sla_to_case(case_id, data.sla_policy_id)

    refusal = _SLA_REFUSALS.get(assignment.outcome)
    if refusal is not None:
        status_code, detail = refusal
        raise HTTPException(
            status_code=status_code,
            detail=detail.format(
                case_id=case_id, policy_id=assignment.policy_id or data.sla_policy_id
            ),
        )

    return CaseSLASchema.dump(assignment.sla)


@router.get("/{case_id}/sla", response_model=Optional[CaseSLAStatusSchema])
async def get_case_sla(case_id: str):
    """Get SLA status for case, or null when the case has no SLA."""
    sla_service = CaseSLAService()
    return sla_service.get_sla_status(case_id)


@router.post(
    "/{case_id}/sla/pause",
    dependencies=_CASES_WRITE,
    response_model=CaseSuccessResponse,
)
async def pause_sla(case_id: str):
    """Pause SLA timer."""
    sla_service = CaseSLAService()
    success = sla_service.pause_sla(case_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to pause SLA")
    return {"success": True}


@router.post(
    "/{case_id}/sla/resume",
    dependencies=_CASES_WRITE,
    response_model=CaseSuccessResponse,
)
async def resume_sla(case_id: str):
    """Resume SLA timer."""
    sla_service = CaseSLAService()
    success = sla_service.resume_sla(case_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to resume SLA")
    return {"success": True}


# Comments and Collaboration
class CommentAdd(BaseModel):
    """Add comment to case."""

    author: Optional[str] = Field(
        default=None,
        description="Ignored: the signed-in user is recorded as the author.",
    )
    content: str
    parent_comment_id: Optional[int] = None


@router.get("/{case_id}/comments", response_model=CaseCommentsResponse)
async def get_comments(case_id: str):
    """Get all comments for case."""
    collab_service = CaseCollaborationService()
    comments = collab_service.get_case_comments(case_id)
    return {"comments": CaseCommentSchema.dump_many(comments)}


@router.post(
    "/{case_id}/comments", dependencies=_CASES_WRITE, response_model=CaseCommentSchema
)
async def add_comment(
    case_id: str,
    data: CommentAdd,
    current_user: User = Depends(get_current_user),
):
    """Add comment to case."""
    collab_service = CaseCollaborationService()
    # The body field stays for clients that still send it; the record names the session user.
    comment = collab_service.add_comment(
        case_id, current_user.username, data.content, data.parent_comment_id
    )
    if not comment:
        raise HTTPException(status_code=500, detail="Failed to add comment")
    return CaseCommentSchema.dump(comment)


class CommentUpdate(BaseModel):
    """Update comment."""

    content: str


@router.put(
    "/{case_id}/comments/{comment_id}",
    dependencies=_CASES_WRITE,
    response_model=CaseSuccessResponse,
)
async def update_comment(
    case_id: str,
    comment_id: int,
    data: CommentUpdate,
    current_user: User = Depends(get_current_user),
):
    """Update a comment on this case. The editor is the signed-in user."""
    collab_service = CaseCollaborationService()
    updated = collab_service.update_comment(
        case_id, comment_id, data.content, current_user.username
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Comment not found")
    return {"success": True}


@router.delete(
    "/{case_id}/comments/{comment_id}",
    dependencies=_CASES_WRITE,
    response_model=CaseSuccessResponse,
)
async def delete_comment(
    case_id: str,
    comment_id: int,
    current_user: User = Depends(get_current_user),
):
    """Delete a comment on this case. Who deleted it is the signed-in user."""
    collab_service = CaseCollaborationService()
    deleted = collab_service.delete_comment(case_id, comment_id, current_user.username)
    if not deleted:
        raise HTTPException(status_code=404, detail="Comment not found")
    return {"success": True}


# Watchers
class WatcherAdd(BaseModel):
    """Add watcher to case."""

    user_id: str
    # Keys are restricted to WATCHER_NOTIFICATION_TYPES: notify_watchers reads
    # this map with ``prefs.get(notification_type, True)``, so any other key is
    # stored and never consulted. Accepting one silently told the caller they
    # had suppressed a notification they will still receive. See #553.
    notification_preferences: Optional[Dict[str, bool]] = None

    @field_validator("notification_preferences")
    @classmethod
    def _known_notification_types(
        cls, v: Optional[Dict[str, bool]]
    ) -> Optional[Dict[str, bool]]:
        unknown = sorted(set(v or {}) - WATCHER_NOTIFICATION_TYPES)
        if unknown:
            raise ValueError(
                f"unknown notification types: {unknown}; "
                f"known types: {sorted(WATCHER_NOTIFICATION_TYPES)}"
            )
        return v


@router.post(
    "/{case_id}/watchers", dependencies=_CASES_WRITE, response_model=CaseWatcherSchema
)
async def add_watcher(case_id: str, data: WatcherAdd):
    """Add watcher to case."""
    collab_service = CaseCollaborationService()
    watcher = collab_service.add_watcher(
        case_id, data.user_id, data.notification_preferences
    )
    if not watcher:
        raise HTTPException(status_code=500, detail="Failed to add watcher")
    return CaseWatcherSchema.dump(watcher)


@router.delete(
    "/{case_id}/watchers/{user_id}",
    dependencies=_CASES_WRITE,
    response_model=CaseSuccessResponse,
)
async def remove_watcher(case_id: str, user_id: str):
    """Remove watcher from case."""
    collab_service = CaseCollaborationService()
    success = collab_service.remove_watcher(case_id, user_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to remove watcher")
    return {"success": True}


@router.get("/{case_id}/watchers", response_model=CaseWatchersResponse)
async def get_watchers(case_id: str):
    """Get all watchers for case."""
    collab_service = CaseCollaborationService()
    watchers = collab_service.get_case_watchers(case_id)
    return {"watchers": CaseWatcherSchema.dump_many(watchers)}


# Evidence Management


class ChainOfCustodyAdd(BaseModel):
    """Add chain of custody entry."""

    action: str
    user: Optional[str] = Field(
        default=None,
        description="Ignored: the signed-in user is recorded as the actor.",
    )
    notes: Optional[str] = None


@router.post(
    "/{case_id}/evidence/{evidence_id}/chain-of-custody",
    dependencies=_CASES_WRITE,
    response_model=CaseSuccessResponse,
)
async def add_custody_entry(
    case_id: str,
    evidence_id: int,
    data: ChainOfCustodyAdd,
    current_user: User = Depends(get_current_user),
):
    """Add chain of custody entry."""
    evidence_service = CaseEvidenceService()
    success = evidence_service.add_chain_of_custody_entry(
        evidence_id, data.action, current_user.username, data.notes
    )
    if not success:
        raise HTTPException(status_code=500, detail="Failed to add custody entry")
    return {"success": True}


# IOC Management


# Task Management
class TaskAdd(BaseModel):
    """Add task to case."""

    title: str
    description: Optional[str] = None
    assignee: Optional[str] = None
    priority: str = "medium"
    due_date: Optional[datetime] = None
    checklist_items: Optional[List[Dict]] = None


@router.post(
    "/{case_id}/tasks", dependencies=_CASES_WRITE, response_model=CaseTaskSchema
)
async def add_task(case_id: str, data: TaskAdd, session: UnitOfWorkSession):
    """Add task to case."""

    task = case_records_service.add_task(
        session,
        case_id,
        title=data.title,
        description=data.description,
        assignee=data.assignee,
        priority=data.priority,
        due_date=data.due_date,
        checklist_items=data.checklist_items,
    )
    return CaseTaskSchema.dump(task)


@router.get("/{case_id}/tasks", response_model=CaseTasksResponse)
async def get_tasks(case_id: str):
    """Get all tasks for case."""
    tasks = case_records_service.list_tasks(case_id)
    return {"tasks": CaseTaskSchema.dump_many(tasks)}


class TaskUpdate(BaseModel):
    """Update task."""

    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    assignee: Optional[str] = None
    priority: Optional[str] = None
    due_date: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    actual_hours: Optional[float] = None


@router.put(
    "/{case_id}/tasks/{task_id}",
    dependencies=_CASES_WRITE,
    response_model=CaseTaskSchema,
)
async def update_task(
    case_id: str, task_id: int, data: TaskUpdate, session: UnitOfWorkSession
):
    """Update task."""

    task = case_records_service.update_task(session, task_id, data.model_dump())
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return CaseTaskSchema.dump(task)


# Case Relationships
class RelationshipAdd(BaseModel):
    """Add case relationship."""

    related_case_id: str
    relationship_type: str
    created_by: Optional[str] = Field(
        default=None,
        description="Ignored: the signed-in user is recorded as the creator.",
    )
    notes: Optional[str] = None


@router.post(
    "/{case_id}/relationships",
    dependencies=_CASES_WRITE,
    response_model=CaseRelationshipSchema,
)
async def add_relationship(
    case_id: str,
    data: RelationshipAdd,
    session: UnitOfWorkSession,
    current_user: User = Depends(get_current_user),
):
    """Link related case."""

    rel = case_records_service.add_relationship(
        session,
        case_id,
        related_case_id=data.related_case_id,
        relationship_type=data.relationship_type,
        created_by=current_user.username,
        notes=data.notes,
    )
    return CaseRelationshipSchema.dump(rel)


@router.get("/{case_id}/relationships", response_model=CaseRelationshipsResponse)
async def get_relationships(case_id: str, session: UnitOfWorkSession):
    """Get related cases."""

    rels = case_records_service.list_relationships(session, case_id)
    return {"relationships": CaseRelationshipSchema.dump_many(rels)}


# Case Closure


# Case Escalation
class EscalationAdd(BaseModel):
    """Escalate case."""

    escalated_from: Optional[str] = Field(
        default=None,
        description="Ignored: the signed-in user is recorded as who escalated.",
    )
    escalated_to: str
    reason: str
    urgency_level: str = "high"


@router.post(
    "/{case_id}/escalate", dependencies=_CASES_WRITE, response_model=CaseSuccessResponse
)
async def escalate_case(
    case_id: str,
    data: EscalationAdd,
    current_user: User = Depends(get_current_user),
):
    """Escalate case. Who escalated it is the signed-in user."""
    workflow_service = CaseWorkflowService()
    success = workflow_service.escalate_case(
        case_id,
        escalated_from=current_user.username,
        escalated_to=data.escalated_to,
        reason=data.reason,
        urgency_level=data.urgency_level,
    )
    if not success:
        raise HTTPException(status_code=500, detail="Failed to escalate case")
    return {"success": True}


@router.get("/{case_id}/escalations", response_model=CaseEscalationsResponse)
async def get_escalations(case_id: str, session: UnitOfWorkSession):
    """Get escalation history."""

    escalations = case_records_service.list_escalations(session, case_id)
    return {"escalations": CaseEscalationSchema.dump_many(escalations)}


async def _run_events(ref: dict) -> tuple[Optional[list], str]:
    """One run's ledger, or None with the reason it could not be read."""
    try:
        events = await read_events(ref["run_id"])
    except Exception as exc:  # noqa: BLE001 — the operator is owed the reason
        logger.error("could not read the record of run %s: %s", ref["run_id"], exc)
        return None, str(exc)
    if events is None:
        return None, "the agent layer has no such run"
    # The agent answers an unknown run with an empty list. A run that has ended
    # wrote at least its start, so empty there is a run the agent never had; a
    # live run may simply not have written yet.
    if not events and not ref.get("live"):
        return None, "the agent layer has no events for it"
    return events, ""


@router.get("/{case_id}/record", response_model=CaseRecordResponse)
async def get_case_record(case_id: str, session: UnitOfWorkSession):
    """The case record: every run's ledger, audit rows and activities.

    Newest first. The agent returns each ledger with snapshots off, read
    concurrently; a run that cannot be read is one row saying why, not a failed
    record. Audit rows are ``entity_type == case`` and ``entity_id`` this case —
    the table has no ``case_id`` column. ``run_id`` is the case's run
    (``primary_ref``), which Replay, Verify chain and Export audit read.
    """
    case = data_service.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    refs = [
        ref
        for ref, _ in case_run_refs(
            case_records_service.list_case_investigations(session, case_id),
            case_records_service.list_case_runs(session, case_id),
        )
    ]
    primary = primary_ref(refs)
    reads = await asyncio.gather(*(_run_events(ref) for ref in refs))
    runs, unread = [], []
    for ref, (events, reason) in zip(refs, reads):
        if events is None:
            unread.append(unread_row(ref["run_id"], ref.get("created_at"), reason))
        else:
            runs.append((ref["run_id"], events))

    audits = case_records_service.list_case_audit(session, case_id)
    return {
        "run_id": primary["run_id"] if primary else None,
        "investigation_id": primary["investigation_id"] if primary else None,
        "rows": merge_record(
            runs, audits, case.get("activities") or [], unread, case_id=case_id
        ),
    }


@router.post(
    "/{case_id}/attachments",
    dependencies=_CASES_WRITE,
    response_model=CaseEvidenceSchema,
)
async def attach_case_document(
    case_id: str,
    session: UnitOfWorkSession,
    file: UploadFile = File(...),
    name: Optional[str] = Form(None),
    pages: int = Form(1, ge=1),
    current_user: User = Depends(get_current_user),
):
    """Keep the original of a document attached to a hunt, as ``document`` evidence.

    Extraction already happened, at ``/workflows/threat-hunt/document``, so this
    stores the file and says nothing about its text.
    """
    if not data_service.get_case(case_id):
        raise HTTPException(status_code=404, detail="Case not found")
    filename = file.filename or "document"
    suffix = Path(filename).suffix.lower()
    if suffix not in ACCEPTED_TYPES:
        raise HTTPException(
            status_code=415, detail=f"{suffix or 'That'} files cannot be attached"
        )
    try:
        path = await spool_upload(file, suffix)
    except DocumentRefused as refused:
        raise HTTPException(status_code=refused.status, detail=refused.reason) from None
    try:
        evidence = attach_document(
            session,
            case_id,
            path,
            filename,
            name=name,
            pages=pages,
            collected_by=current_user.username or current_user.user_id,
        )
    except Exception as exc:  # noqa: BLE001 -- the reason is in the log
        logger.error("could not attach a document to %s: %s", case_id, exc)
        raise HTTPException(
            status_code=500, detail="The document could not be kept on the case"
        ) from None
    finally:
        path.unlink(missing_ok=True)
    return CaseEvidenceSchema.dump(evidence)


# Case Merge


# Advanced Search
