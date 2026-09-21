"""Cases API endpoints."""

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, field_validator

from core.auth.auth_service import AuthService
from core.cases import case_records_service
from core.cases.case_collaboration_service import CaseCollaborationService
from core.cases.case_evidence_service import CaseEvidenceService
from core.cases.case_notification_service import WATCHER_NOTIFICATION_TYPES
from core.cases.case_sla_service import CaseSLAService
from core.reporting.report_service import REPORTLAB_AVAILABLE, ReportService
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.database_data_service import DatabaseDataService
from core.storage.models import User
from core.storage.schemas import (
    CaseCommentSchema,
    CaseEscalationSchema,
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
    CaseRelationshipsResponse,
    CaseReportResponse,
    CaseSuccessResponse,
    CaseTasksResponse,
    CaseWatchersResponse,
)
from core.time import utcnow
from services.api.middleware.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/cases",
    tags=["cases"],
    auth=Auth.REQUIRED,
)
data_service = DatabaseDataService()
if REPORTLAB_AVAILABLE:
    report_service = ReportService()
else:
    report_service = None


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


@router.delete("/all", response_model=CasePurgeResponse)
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


@router.post("/{case_id}/activities", response_model=CaseSchema)
async def add_case_activity(case_id: str, activity: ActivityAdd):
    """
    Add an activity/action to a case.

    Args:
        case_id: The case ID
        activity: Activity data

    Returns:
        Updated case
    """
    case = data_service.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    # Get or initialize activities list
    activities = case.get("activities", [])

    # Add new activity
    new_activity = {
        "timestamp": utcnow().isoformat() + "Z",
        "activity_type": activity.activity_type,
        "description": activity.description,
        "details": activity.details or {},
    }
    activities.append(new_activity)

    # Update case
    success = data_service.update_case(case_id, activities=activities)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to add activity")

    return data_service.get_case(case_id)


@router.post("/{case_id}/resolution-steps", response_model=CaseSchema)
async def add_resolution_step(case_id: str, step: ResolutionStepAdd):
    """
    Add a resolution step to a case.

    Args:
        case_id: The case ID
        step: Resolution step data

    Returns:
        Updated case
    """
    case = data_service.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    # Get or initialize resolution steps list
    resolution_steps = case.get("resolution_steps", [])

    # Add new step
    new_step = {
        "timestamp": utcnow().isoformat() + "Z",
        "description": step.description,
        "action_taken": step.action_taken,
        "result": step.result,
    }
    resolution_steps.append(new_step)

    # Update case
    success = data_service.update_case(case_id, resolution_steps=resolution_steps)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to add resolution step")

    return data_service.get_case(case_id)


@router.post("/{case_id}/generate-report", response_model=CaseReportResponse)
async def generate_case_report(case_id: str):
    """
    Generate a PDF report for a case.

    Args:
        case_id: The case ID

    Returns:
        Report file information
    """
    if not report_service:
        raise HTTPException(
            status_code=501,
            detail="Report generation requires reportlab. Install with: pip install reportlab",
        )

    case = data_service.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    # Get associated findings
    finding_ids = case.get("finding_ids", [])
    findings = [data_service.get_finding(fid) for fid in finding_ids]
    findings = [f for f in findings if f]  # Filter out None values

    # Generate report filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{case_id}_report_{timestamp}.pdf"
    output_path = Path("TestOutputs") / filename
    output_path.parent.mkdir(exist_ok=True)

    # Generate the report
    success = report_service.generate_case_report(output_path, case, findings)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to generate report")

    return {
        "success": True,
        "filename": filename,
        "path": str(output_path),
        "case_id": case_id,
    }


@router.delete("/{case_id}", response_model=CaseSuccessResponse)
async def delete_case(case_id: str, session: UnitOfWorkSession):
    """Delete a case that has no live Investigation (#1001)."""
    # Demo cases live in memory, not in the session, and nothing investigates
    # them, so there is no live run to guard against.
    if data_service.is_demo_mode():
        if not data_service.delete_case(case_id):
            raise HTTPException(status_code=404, detail="Case not found")
        return {"success": True}

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

    return {"success": True}


# =============================================================================
# Enhanced Case Management Endpoints
# =============================================================================


# SLA Management
class SLAAssign(BaseModel):
    """Assign SLA to case."""

    sla_policy_id: Optional[str] = None


@router.post("/{case_id}/sla", response_model=CaseSLASchema)
async def assign_sla(case_id: str, data: SLAAssign):
    """Assign SLA policy to case."""
    sla_service = CaseSLAService()
    result = sla_service.assign_sla_to_case(case_id, data.sla_policy_id)
    if not result:
        raise HTTPException(status_code=500, detail="Failed to assign SLA")
    return CaseSLASchema.dump(result)


@router.get("/{case_id}/sla", response_model=CaseSLAStatusSchema)
async def get_case_sla(case_id: str):
    """Get SLA status for case."""
    sla_service = CaseSLAService()
    status = sla_service.get_sla_status(case_id)
    if not status:
        raise HTTPException(status_code=404, detail="No SLA found for case")
    return status


@router.post("/{case_id}/sla/pause", response_model=CaseSuccessResponse)
async def pause_sla(case_id: str):
    """Pause SLA timer."""
    sla_service = CaseSLAService()
    success = sla_service.pause_sla(case_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to pause SLA")
    return {"success": True}


@router.post("/{case_id}/sla/resume", response_model=CaseSuccessResponse)
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

    author: str
    content: str
    parent_comment_id: Optional[int] = None


@router.get("/{case_id}/comments", response_model=CaseCommentsResponse)
async def get_comments(case_id: str):
    """Get all comments for case."""
    collab_service = CaseCollaborationService()
    comments = collab_service.get_case_comments(case_id)
    return {"comments": CaseCommentSchema.dump_many(comments)}


@router.post("/{case_id}/comments", response_model=CaseCommentSchema)
async def add_comment(case_id: str, data: CommentAdd):
    """Add comment to case."""
    collab_service = CaseCollaborationService()
    comment = collab_service.add_comment(
        case_id, data.author, data.content, data.parent_comment_id
    )
    if not comment:
        raise HTTPException(status_code=500, detail="Failed to add comment")
    return CaseCommentSchema.dump(comment)


class CommentUpdate(BaseModel):
    """Update comment."""

    content: str


@router.put("/{case_id}/comments/{comment_id}", response_model=CaseSuccessResponse)
async def update_comment(case_id: str, comment_id: int, data: CommentUpdate):
    """Update comment."""
    collab_service = CaseCollaborationService()
    success = collab_service.update_comment(comment_id, data.content)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to update comment")
    return {"success": True}


@router.delete("/{case_id}/comments/{comment_id}", response_model=CaseSuccessResponse)
async def delete_comment(case_id: str, comment_id: int):
    """Delete comment."""
    collab_service = CaseCollaborationService()
    success = collab_service.delete_comment(comment_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete comment")
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


@router.post("/{case_id}/watchers", response_model=CaseWatcherSchema)
async def add_watcher(case_id: str, data: WatcherAdd):
    """Add watcher to case."""
    collab_service = CaseCollaborationService()
    watcher = collab_service.add_watcher(
        case_id, data.user_id, data.notification_preferences
    )
    if not watcher:
        raise HTTPException(status_code=500, detail="Failed to add watcher")
    return CaseWatcherSchema.dump(watcher)


@router.delete("/{case_id}/watchers/{user_id}", response_model=CaseSuccessResponse)
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
    user: str
    notes: Optional[str] = None


@router.post(
    "/{case_id}/evidence/{evidence_id}/chain-of-custody",
    response_model=CaseSuccessResponse,
)
async def add_custody_entry(case_id: str, evidence_id: int, data: ChainOfCustodyAdd):
    """Add chain of custody entry."""
    evidence_service = CaseEvidenceService()
    success = evidence_service.add_chain_of_custody_entry(
        evidence_id, data.action, data.user, data.notes
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


@router.post("/{case_id}/tasks", response_model=CaseTaskSchema)
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


@router.put("/{case_id}/tasks/{task_id}", response_model=CaseTaskSchema)
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
    created_by: str
    notes: Optional[str] = None


@router.post("/{case_id}/relationships", response_model=CaseRelationshipSchema)
async def add_relationship(
    case_id: str, data: RelationshipAdd, session: UnitOfWorkSession
):
    """Link related case."""

    rel = case_records_service.add_relationship(
        session,
        case_id,
        related_case_id=data.related_case_id,
        relationship_type=data.relationship_type,
        created_by=data.created_by,
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

    escalated_from: str
    escalated_to: str
    reason: str
    urgency_level: str = "high"


@router.post("/{case_id}/escalate", response_model=CaseSuccessResponse)
async def escalate_case(case_id: str, data: EscalationAdd):
    """Escalate case."""
    from core.cases.case_workflow_service import CaseWorkflowService

    workflow_service = CaseWorkflowService()
    success = workflow_service.escalate_case(
        case_id, data.escalated_from, data.escalated_to, data.reason, data.urgency_level
    )
    if not success:
        raise HTTPException(status_code=500, detail="Failed to escalate case")
    return {"success": True}


@router.get("/{case_id}/escalations", response_model=CaseEscalationsResponse)
async def get_escalations(case_id: str, session: UnitOfWorkSession):
    """Get escalation history."""

    escalations = case_records_service.list_escalations(session, case_id)
    return {"escalations": CaseEscalationSchema.dump_many(escalations)}


# Case Merge


# Advanced Search
