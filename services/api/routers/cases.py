"""Cases API endpoints."""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from core.auth.auth_service import AuthService
from core.cases import case_journal_service, case_records_service
from core.cases.case_collaboration_service import CaseCollaborationService
from core.cases.case_evidence_service import CaseEvidenceService
from core.cases.case_ioc_service import CaseIOCService
from core.cases.case_notification_service import WATCHER_NOTIFICATION_TYPES
from core.cases.case_sla_service import CaseSLAService
from core.cases.closure import ClosedByKind, ClosureCategory
from core.reporting.report_service import REPORTLAB_AVAILABLE, ReportService
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.database_data_service import DatabaseDataService
from core.storage.models import User
from core.storage.schemas import (
    CaseClosureInfoSchema,
    CaseCommentSchema,
    CaseEscalationSchema,
    CaseEvidenceSchema,
    CaseIOCSchema,
    CaseRelationshipSchema,
    CaseSchema,
    CaseSLASchema,
    CaseSLAStatusSchema,
    CaseTaskSchema,
    CaseWatcherSchema,
)
from core.storage.schemas.case_api import (
    CaseCloseResponse,
    CaseCommentsResponse,
    CaseEscalationsResponse,
    CaseEvidenceListResponse,
    CaseIOCBulkResponse,
    CaseIOCExportResponse,
    CaseIOCListResponse,
    CaseListResponse,
    CaseMergeResponse,
    CasePurgeResponse,
    CaseRelationshipsResponse,
    CaseReportResponse,
    CaseSearchResponse,
    CaseSuccessResponse,
    CaseSummaryResponse,
    CaseTasksResponse,
    CaseWatchersResponse,
)
from core.time import utcnow
from services.api.middleware.auth import get_current_user

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


class CaseCreate(BaseModel):
    """Case creation request."""

    title: str
    description: str = ""
    finding_ids: List[str]
    priority: str = "medium"
    status: str = "open"


class CaseUpdate(BaseModel):
    """Case update request."""

    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    notes: Optional[str] = None
    assignee: Optional[str] = None


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


@router.get("/", response_model=CaseListResponse)
async def get_cases(status: Optional[str] = None, priority: Optional[str] = None):
    """
    Get all cases with optional filters.

    Args:
        status: Filter by status
        priority: Filter by priority

    Returns:
        List of cases
    """
    cases = data_service.get_cases()

    # Apply filters
    if status:
        cases = [c for c in cases if c.get("status") == status]
    if priority:
        cases = [c for c in cases if c.get("priority") == priority]

    return {"cases": cases, "total": len(cases)}


@router.delete("/all", response_model=CasePurgeResponse)
async def clear_all_cases(
    session: UnitOfWorkSession,
    current_user: User = Depends(get_current_user),
):
    """Delete all cases and case-derived generated data (requires cases.delete)."""
    if not AuthService.check_permission(current_user.user_id, "cases.delete"):
        raise HTTPException(
            status_code=403, detail="Permission denied: cases.delete required"
        )

    count = case_records_service.purge_all_cases(session)

    return {
        "success": True,
        "deleted": count,
        "message": f"Deleted {count} cases and case-derived records",
    }


@router.get("/{case_id}", response_model=CaseSchema)
async def get_case(case_id: str):
    """
    Get a specific case by ID.

    Args:
        case_id: The case ID

    Returns:
        Case details
    """
    case = data_service.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


@router.post("/", response_model=CaseSchema)
async def create_case(case_data: CaseCreate):
    """
    Create a new case.

    Args:
        case_data: Case creation data

    Returns:
        Created case
    """
    case = data_service.create_case(
        title=case_data.title,
        finding_ids=case_data.finding_ids,
        priority=case_data.priority,
        description=case_data.description,
        status=case_data.status,
    )

    if not case:
        raise HTTPException(status_code=500, detail="Failed to create case")

    # Automatically assign SLA policy based on priority
    try:
        from core.cases.case_sla_service import CaseSLAService

        sla_service = CaseSLAService()

        case_id = case.get("case_id")
        if case_id:
            # This will auto-select the default policy for the case priority
            sla_result = sla_service.assign_sla_to_case(case_id, sla_policy_id=None)
            if sla_result:
                import logging

                logger = logging.getLogger(__name__)
                logger.info(f"Auto-assigned SLA policy to case {case_id}")
    except Exception as e:
        # Don't fail case creation if SLA assignment fails
        import logging

        logger = logging.getLogger(__name__)
        logger.warning(f"Failed to auto-assign SLA to case {case.get('case_id')}: {e}")

    return case


async def _sync_upstream_status(case_id: str, new_status: str) -> None:
    """Best-effort sync of case status to the upstream SIEM."""
    import logging

    _logger = logging.getLogger(__name__)
    try:
        case = data_service.get_case(case_id)
        if not case:
            return
        # Only sync findings that came from a SIEM with upstream support
        finding_ids = case.get("finding_ids", [])
        for fid in finding_ids:
            finding = data_service.get_finding(fid)
            if not finding:
                continue
            source = finding.get("data_source", "")
            alert_id = (finding.get("metadata") or {}).get(f"{source}_alert_id") or (
                finding.get("metadata") or {}
            ).get("elastic_alert_id")
            if not alert_id:
                continue
            # Lazy-load the right ingestion service
            svc = _get_ingestion_service(source)
            if svc is None:
                continue
            try:
                await svc.update_upstream_alert_status(alert_id, new_status)
                _logger.info(
                    f"Synced status '{new_status}' to {source} alert {alert_id}"
                )
            except NotImplementedError:
                pass
            except Exception as exc:
                _logger.warning(
                    f"Failed to sync status to {source} alert {alert_id}: {exc}"
                )
    except Exception as exc:
        import logging

        logging.getLogger(__name__).warning(
            f"Upstream status sync error for case {case_id}: {exc}"
        )


def _get_ingestion_service(source: str):
    """Return the ingestion service for a given data source, or None."""
    if source == "elastic":
        try:
            from core.integrations.elastic.ingestion import ElasticIngestion

            return ElasticIngestion()
        except Exception:
            return None
    # Future: add splunk, crowdstrike, etc.
    return None


def _record_status_close(session, case_id: str, closed_by: str) -> None:
    """Record who closed a Case that was closed by editing its status.

    This is how the console closes a Case: it PATCHes the status and asks for no
    category. Left unrecorded, the close that matters most -- a person looked and
    said no -- reaches episodic memory as nothing at all, because there is no
    closure row for the Case Distil to read a category or an actor from.

    The category is ``unspecified``, which is not a determination and does not
    pretend to be one: it says the Case was closed and no reason was stated, and
    becomes an inconclusive Verdict rather than a claim nobody made. It goes
    through ``close_case`` like every other close, so this path stops the SLA
    resolution clock and indexes the Case's IOCs as the others do, and cannot
    overwrite a determination an earlier close already stated.

    This lands in the request's own transaction while the status change went
    through ``data_service`` in its own, so a failure here 500s with the Case
    already closed and no closure row. The Distil reads that as a close with no
    stated reason -- the Verdict is still written, at Trust ``agent`` rather
    than ``analyst``. Degraded, and never a Case that closed and vanished.
    """
    from core.cases.case_workflow_service import CaseWorkflowService

    CaseWorkflowService().close_case(
        session,
        case_id,
        closure_category=ClosureCategory.UNSPECIFIED,
        closed_by=closed_by,
        closed_by_kind=ClosedByKind.ANALYST,
    )


@router.patch("/{case_id}", response_model=CaseSuccessResponse)
async def update_case(
    case_id: str,
    case_data: CaseUpdate,
    session: UnitOfWorkSession,
    current_user: User = Depends(get_current_user),
):
    """
    Update an existing case.

    Args:
        case_id: The case ID
        case_data: Case update data

    Returns:
        Success status
    """
    # Build updates dict
    updates = {}
    if case_data.title is not None:
        updates["title"] = case_data.title
    if case_data.description is not None:
        updates["description"] = case_data.description
    if case_data.status is not None:
        updates["status"] = case_data.status
    if case_data.priority is not None:
        updates["priority"] = case_data.priority
    if case_data.notes is not None:
        case = data_service.get_case(case_id)
        if not case:
            raise HTTPException(status_code=404, detail="Case not found")
        notes = case.get("notes") or []
        notes.append(
            {
                "timestamp": utcnow().isoformat() + "Z",
                "content": case_data.notes,
            }
        )
        updates["notes"] = notes

    # Read before the write, because what makes this a close is the transition:
    # re-PATCHing `closed` onto an already-closed Case is an edit, and stamping
    # it would move the closure's date and re-derive its Verdict for nothing.
    was_closed = (data_service.get_case(case_id) or {}).get("status") == "closed"

    success = data_service.update_case(case_id, **updates)

    if not success:
        raise HTTPException(status_code=404, detail="Case not found or update failed")

    if updates.get("status") == "closed" and not was_closed:
        _record_status_close(session, case_id, current_user.username)
    elif was_closed and updates.get("status") not in (None, "closed"):
        from core.cases.case_workflow_service import CaseWorkflowService

        CaseWorkflowService().reopen_case(session, case_id)

    # Fire upstream SIEM status sync when status changes
    if case_data.status is not None:
        import asyncio

        asyncio.ensure_future(_sync_upstream_status(case_id, case_data.status))

    return {"success": True}


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


@router.post("/{case_id}/findings/{finding_id}", response_model=CaseSchema)
async def add_finding_to_case(case_id: str, finding_id: str):
    """
    Add a finding to a case.

    Args:
        case_id: The case ID
        finding_id: The finding ID to add

    Returns:
        Updated case
    """
    linked = case_journal_service.link_finding(data_service, case_id, finding_id)
    if linked is None:
        if not data_service.get_case(case_id):
            raise HTTPException(status_code=404, detail="Case not found")
        raise HTTPException(status_code=500, detail="Failed to add finding")

    return data_service.get_case(case_id)


@router.delete("/{case_id}/findings/{finding_id}", response_model=CaseSchema)
async def remove_finding_from_case(case_id: str, finding_id: str):
    """
    Remove a finding from a case.

    Args:
        case_id: The case ID
        finding_id: The finding ID to remove

    Returns:
        Updated case
    """
    unlinked = case_journal_service.unlink_finding(data_service, case_id, finding_id)
    if unlinked is None:
        if not data_service.get_case(case_id):
            raise HTTPException(status_code=404, detail="Case not found")
        raise HTTPException(status_code=500, detail="Failed to remove finding")

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
async def delete_case(case_id: str):
    """
    Delete a case.

    Args:
        case_id: The case ID

    Returns:
        Success status
    """
    case = data_service.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    success = data_service.delete_case(case_id)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete case")

    return {"success": True}


@router.get("/stats/summary", response_model=CaseSummaryResponse)
async def get_cases_summary():
    """
    Get summary statistics for cases.

    Returns:
        Summary statistics
    """
    cases = data_service.get_cases()

    # Calculate statistics
    status_counts = {}
    priority_counts = {}
    total_count = len(cases)

    for case in cases:
        status = case.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

        priority = case.get("priority", "unknown")
        priority_counts[priority] = priority_counts.get(priority, 0) + 1

    return {
        "total": total_count,
        "by_status": status_counts,
        "by_priority": priority_counts,
    }


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
class EvidenceAdd(BaseModel):
    """Add evidence to case."""

    evidence_type: str
    name: str
    collected_by: str
    description: Optional[str] = None
    file_path: Optional[str] = None
    source: Optional[str] = None
    tags: Optional[List[str]] = None


@router.post("/{case_id}/evidence", response_model=CaseEvidenceSchema)
async def add_evidence(case_id: str, data: EvidenceAdd):
    """Add evidence to case."""
    evidence_service = CaseEvidenceService()
    evidence = evidence_service.add_evidence(
        case_id=case_id,
        evidence_type=data.evidence_type,
        name=data.name,
        collected_by=data.collected_by,
        description=data.description,
        file_path=data.file_path,
        source=data.source,
        tags=data.tags,
    )
    if not evidence:
        raise HTTPException(status_code=500, detail="Failed to add evidence")
    return CaseEvidenceSchema.dump(evidence)


@router.get("/{case_id}/evidence", response_model=CaseEvidenceListResponse)
async def get_evidence(case_id: str, evidence_type: Optional[str] = None):
    """Get all evidence for case."""
    evidence_service = CaseEvidenceService()
    evidence_list = evidence_service.get_case_evidence(case_id, evidence_type)
    return {"evidence": CaseEvidenceSchema.dump_many(evidence_list)}


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
class IOCAdd(BaseModel):
    """Add IOC to case."""

    ioc_type: str
    value: str
    threat_level: Optional[str] = None
    confidence: Optional[float] = None
    source: Optional[str] = None
    tags: Optional[List[str]] = None
    context: Optional[str] = None


@router.post("/{case_id}/iocs", response_model=CaseIOCSchema)
async def add_ioc(case_id: str, data: IOCAdd):
    """Add IOC to case."""
    ioc_service = CaseIOCService()
    ioc = ioc_service.add_ioc(
        case_id=case_id,
        ioc_type=data.ioc_type,
        value=data.value,
        threat_level=data.threat_level,
        confidence=data.confidence,
        source=data.source,
        tags=data.tags,
        context=data.context,
    )
    if not ioc:
        raise HTTPException(status_code=500, detail="Failed to add IOC")
    return CaseIOCSchema.dump(ioc)


@router.get("/{case_id}/iocs", response_model=CaseIOCListResponse)
async def get_iocs(case_id: str, ioc_type: Optional[str] = None):
    """Get all IOCs for case."""
    ioc_service = CaseIOCService()
    iocs = ioc_service.get_case_iocs(case_id, ioc_type)
    return {"iocs": CaseIOCSchema.dump_many(iocs)}


class IOCBulkAdd(BaseModel):
    """Bulk add IOCs."""

    iocs: List[Dict]


@router.post("/{case_id}/iocs/bulk", response_model=CaseIOCBulkResponse)
async def bulk_add_iocs(case_id: str, data: IOCBulkAdd):
    """Bulk add IOCs to case."""
    ioc_service = CaseIOCService()
    count = ioc_service.bulk_add_iocs(case_id, data.iocs)
    return {"added": count}


@router.get("/{case_id}/iocs/export", response_model=CaseIOCExportResponse)
async def export_iocs(case_id: str, format: str = "json"):
    """Export IOCs (json, csv, or stix)."""
    ioc_service = CaseIOCService()

    if format == "csv":
        content = ioc_service.export_iocs_csv(case_id)
        return {"format": "csv", "content": content}
    elif format == "stix":
        content = ioc_service.export_iocs_stix(case_id)
        return {"format": "stix", "content": content}
    else:
        content = ioc_service.export_iocs_json(case_id)
        return {"format": "json", "content": content}


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
class ClosureInfo(BaseModel):
    """Close case with metadata.

    No ``closed_by``: who closed it is the authenticated principal, not
    something a client says about itself. Episodic memory reads it as Trust
    (#733), and a client-supplied name would let any caller claim an analyst
    concluded.
    """

    closure_category: ClosureCategory
    root_cause: Optional[str] = None
    lessons_learned: Optional[str] = None
    recommendations: Optional[str] = None
    executive_summary: Optional[str] = None
    false_positive_reason: Optional[str] = None
    closure_notes: Optional[str] = None


@router.post("/{case_id}/close", response_model=CaseCloseResponse)
async def close_case(
    case_id: str,
    data: ClosureInfo,
    session: UnitOfWorkSession,
    current_user: User = Depends(get_current_user),
):
    """Close case with closure metadata."""

    from core.cases.case_workflow_service import CaseWorkflowService

    closure = CaseWorkflowService().close_case(
        session,
        case_id,
        closure_category=data.closure_category,
        closed_by=current_user.username,
        closed_by_kind=ClosedByKind.ANALYST,
        root_cause=data.root_cause,
        lessons_learned=data.lessons_learned,
        recommendations=data.recommendations,
        executive_summary=data.executive_summary,
        false_positive_reason=data.false_positive_reason,
        closure_notes=data.closure_notes,
    )
    if not closure:
        raise HTTPException(status_code=404, detail="Case not found")

    # The PATCH route has always told the upstream SIEM when a status changed, and
    # this one never did: a Case closed here, by either MCP tool or by a merge,
    # stayed open in the SIEM that raised it. Best-effort and fire-and-forget, as
    # it is there -- the close is recorded either way.
    import asyncio

    asyncio.ensure_future(_sync_upstream_status(case_id, "closed"))
    return {"success": True, "closure": CaseClosureInfoSchema.dump(closure)}


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
class MergeRequest(BaseModel):
    """Merge another case into this one."""

    source_case_id: str
    merged_by: str = "system"


@router.post("/{case_id}/merge", response_model=CaseMergeResponse)
async def merge_cases(case_id: str, data: MergeRequest):
    """Merge source case into target case.

    Moves all findings, timeline entries, activities, IOCs, evidence, tasks,
    and comments from the source case into the target. The source case is
    closed with a note and linked via a 'merged_into' relationship.
    """
    if case_id == data.source_case_id:
        raise HTTPException(status_code=400, detail="Cannot merge a case into itself")

    from core.cases.case_workflow_service import CaseWorkflowService

    # A missing case surfaces as NotFoundError, which the shared handler
    # renders as a 404 naming which of the two it was.
    moved_findings = CaseWorkflowService().merge_cases(
        case_id, data.source_case_id, data.merged_by
    )

    result_case = data_service.get_case(case_id)
    return {
        "success": True,
        "target_case": result_case,
        "findings_moved": moved_findings,
        "source_case_status": "closed",
        "message": f"Case {data.source_case_id} merged into {case_id}",
    }


# Advanced Search
class SearchRequest(BaseModel):
    """Advanced search request."""

    query_text: Optional[str] = None
    status: Optional[List[str]] = None
    priority: Optional[List[str]] = None
    assignee: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    mitre_techniques: Optional[List[str]] = None
    created_after: Optional[datetime] = None
    created_before: Optional[datetime] = None
    limit: int = 100
    offset: int = 0


@router.post("/search", response_model=CaseSearchResponse)
async def search_cases(data: SearchRequest):
    """Advanced case search."""
    from core.cases.case_search_service import CaseSearchService

    search_service = CaseSearchService()

    results = search_service.search_cases(
        query_text=data.query_text,
        status=data.status,
        priority=data.priority,
        assignee=data.assignee,
        tags=data.tags,
        mitre_techniques=data.mitre_techniques,
        created_after=data.created_after,
        created_before=data.created_before,
        limit=data.limit,
        offset=data.offset,
    )
    return results
