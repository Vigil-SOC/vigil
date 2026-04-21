"""Cases API endpoints."""

from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from datetime import datetime
from pathlib import Path

from services.database_data_service import DatabaseDataService
from services.report_service import ReportService, REPORTLAB_AVAILABLE

router = APIRouter()
# Use DatabaseDataService which automatically uses PostgreSQL if available, falls back to JSON
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


@router.get("/")
async def get_cases(
    status: Optional[str] = None,
    priority: Optional[str] = None
):
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
        cases = [c for c in cases if c.get('status') == status]
    if priority:
        cases = [c for c in cases if c.get('priority') == priority]
    
    return {"cases": cases, "total": len(cases)}


@router.get("/{case_id}")
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


@router.post("/")
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
        status=case_data.status
    )
    
    if not case:
        raise HTTPException(status_code=500, detail="Failed to create case")
    
    # Automatically assign SLA policy based on priority
    try:
        from services.case_sla_service import CaseSLAService
        sla_service = CaseSLAService()
        
        case_id = case.get('case_id')
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
            alert_id = (finding.get("metadata") or {}).get(
                f"{source}_alert_id"
            ) or (finding.get("metadata") or {}).get("elastic_alert_id")
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
            from services.elastic_ingestion import ElasticIngestion
            return ElasticIngestion()
        except Exception:
            return None
    # Future: add splunk, crowdstrike, etc.
    return None


@router.patch("/{case_id}")
async def update_case(case_id: str, case_data: CaseUpdate):
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
        updates['title'] = case_data.title
    if case_data.description is not None:
        updates['description'] = case_data.description
    if case_data.status is not None:
        updates['status'] = case_data.status
    if case_data.priority is not None:
        updates['priority'] = case_data.priority
    if case_data.notes is not None:
        updates['notes'] = case_data.notes
    
    success = data_service.update_case(case_id, **updates)

    if not success:
        raise HTTPException(status_code=404, detail="Case not found or update failed")

    # Fire upstream SIEM status sync when status changes
    if case_data.status is not None:
        import asyncio
        asyncio.ensure_future(
            _sync_upstream_status(case_id, case_data.status)
        )

    return {"success": True}


@router.post("/{case_id}/activities")
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
    activities = case.get('activities', [])
    
    # Add new activity
    new_activity = {
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'activity_type': activity.activity_type,
        'description': activity.description,
        'details': activity.details or {}
    }
    activities.append(new_activity)
    
    # Update case
    success = data_service.update_case(case_id, activities=activities)
    
    if not success:
        raise HTTPException(status_code=500, detail="Failed to add activity")
    
    return data_service.get_case(case_id)


@router.post("/{case_id}/resolution-steps")
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
    resolution_steps = case.get('resolution_steps', [])
    
    # Add new step
    new_step = {
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'description': step.description,
        'action_taken': step.action_taken,
        'result': step.result
    }
    resolution_steps.append(new_step)
    
    # Update case
    success = data_service.update_case(case_id, resolution_steps=resolution_steps)
    
    if not success:
        raise HTTPException(status_code=500, detail="Failed to add resolution step")
    
    return data_service.get_case(case_id)


@router.post("/{case_id}/findings/{finding_id}")
async def add_finding_to_case(case_id: str, finding_id: str):
    """
    Add a finding to a case.
    
    Args:
        case_id: The case ID
        finding_id: The finding ID to add
    
    Returns:
        Updated case
    """
    case = data_service.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    
    finding_ids = case.get('finding_ids', [])
    if finding_id not in finding_ids:
        finding_ids.append(finding_id)
        success = data_service.update_case(case_id, finding_ids=finding_ids)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to add finding")
    
    return data_service.get_case(case_id)


@router.delete("/{case_id}/findings/{finding_id}")
async def remove_finding_from_case(case_id: str, finding_id: str):
    """
    Remove a finding from a case.
    
    Args:
        case_id: The case ID
        finding_id: The finding ID to remove
    
    Returns:
        Updated case
    """
    case = data_service.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    
    finding_ids = case.get('finding_ids', [])
    if finding_id in finding_ids:
        finding_ids.remove(finding_id)
        success = data_service.update_case(case_id, finding_ids=finding_ids)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to remove finding")
    
    return data_service.get_case(case_id)


@router.post("/{case_id}/generate-report")
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
            detail="Report generation requires reportlab. Install with: pip install reportlab"
        )
    
    case = data_service.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    
    # Get associated findings
    finding_ids = case.get('finding_ids', [])
    findings = [data_service.get_finding(fid) for fid in finding_ids]
    findings = [f for f in findings if f]  # Filter out None values
    
    # Generate report filename
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
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
        "case_id": case_id
    }


@router.delete("/{case_id}")
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


@router.get("/stats/summary")
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
        status = case.get('status', 'unknown')
        status_counts[status] = status_counts.get(status, 0) + 1
        
        priority = case.get('priority', 'unknown')
        priority_counts[priority] = priority_counts.get(priority, 0) + 1
    
    return {
        "total": total_count,
        "by_status": status_counts,
        "by_priority": priority_counts
    }


# =============================================================================
# Enhanced Case Management Endpoints
# =============================================================================

# SLA Management
class SLAAssign(BaseModel):
    """Assign SLA to case."""
    sla_policy_id: Optional[str] = None


@router.post("/{case_id}/sla")
async def assign_sla(case_id: str, data: SLAAssign):
    """Assign SLA policy to case."""
    from services.case_sla_service import CaseSLAService
    sla_service = CaseSLAService()
    result = sla_service.assign_sla_to_case(case_id, data.sla_policy_id)
    if not result:
        raise HTTPException(status_code=500, detail="Failed to assign SLA")
    return result.to_dict()


@router.get("/{case_id}/sla")
async def get_case_sla(case_id: str):
    """Get SLA status for case."""
    from services.case_sla_service import CaseSLAService
    sla_service = CaseSLAService()
    status = sla_service.get_sla_status(case_id)
    if not status:
        raise HTTPException(status_code=404, detail="No SLA found for case")
    return status


@router.post("/{case_id}/sla/pause")
async def pause_sla(case_id: str):
    """Pause SLA timer."""
    from services.case_sla_service import CaseSLAService
    sla_service = CaseSLAService()
    success = sla_service.pause_sla(case_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to pause SLA")
    return {"success": True}


@router.post("/{case_id}/sla/resume")
async def resume_sla(case_id: str):
    """Resume SLA timer."""
    from services.case_sla_service import CaseSLAService
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


@router.get("/{case_id}/comments")
async def get_comments(case_id: str):
    """Get all comments for case."""
    from services.case_collaboration_service import CaseCollaborationService
    collab_service = CaseCollaborationService()
    comments = collab_service.get_case_comments(case_id)
    return {"comments": [c.to_dict() for c in comments]}


@router.post("/{case_id}/comments")
async def add_comment(case_id: str, data: CommentAdd):
    """Add comment to case."""
    from services.case_collaboration_service import CaseCollaborationService
    collab_service = CaseCollaborationService()
    comment = collab_service.add_comment(
        case_id, data.author, data.content, data.parent_comment_id
    )
    if not comment:
        raise HTTPException(status_code=500, detail="Failed to add comment")
    return comment.to_dict()


class CommentUpdate(BaseModel):
    """Update comment."""
    content: str


@router.put("/{case_id}/comments/{comment_id}")
async def update_comment(case_id: str, comment_id: int, data: CommentUpdate):
    """Update comment."""
    from services.case_collaboration_service import CaseCollaborationService
    collab_service = CaseCollaborationService()
    success = collab_service.update_comment(comment_id, data.content)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to update comment")
    return {"success": True}


@router.delete("/{case_id}/comments/{comment_id}")
async def delete_comment(case_id: str, comment_id: int):
    """Delete comment."""
    from services.case_collaboration_service import CaseCollaborationService
    collab_service = CaseCollaborationService()
    success = collab_service.delete_comment(comment_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete comment")
    return {"success": True}


# Watchers
class WatcherAdd(BaseModel):
    """Add watcher to case."""
    user_id: str
    notification_preferences: Optional[Dict] = None


@router.post("/{case_id}/watchers")
async def add_watcher(case_id: str, data: WatcherAdd):
    """Add watcher to case."""
    from services.case_collaboration_service import CaseCollaborationService
    collab_service = CaseCollaborationService()
    watcher = collab_service.add_watcher(
        case_id, data.user_id, data.notification_preferences
    )
    if not watcher:
        raise HTTPException(status_code=500, detail="Failed to add watcher")
    return watcher.to_dict()


@router.delete("/{case_id}/watchers/{user_id}")
async def remove_watcher(case_id: str, user_id: str):
    """Remove watcher from case."""
    from services.case_collaboration_service import CaseCollaborationService
    collab_service = CaseCollaborationService()
    success = collab_service.remove_watcher(case_id, user_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to remove watcher")
    return {"success": True}


@router.get("/{case_id}/watchers")
async def get_watchers(case_id: str):
    """Get all watchers for case."""
    from services.case_collaboration_service import CaseCollaborationService
    collab_service = CaseCollaborationService()
    watchers = collab_service.get_case_watchers(case_id)
    return {"watchers": [w.to_dict() for w in watchers]}


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


@router.post("/{case_id}/evidence")
async def add_evidence(case_id: str, data: EvidenceAdd):
    """Add evidence to case."""
    from services.case_evidence_service import CaseEvidenceService
    evidence_service = CaseEvidenceService()
    evidence = evidence_service.add_evidence(
        case_id=case_id,
        evidence_type=data.evidence_type,
        name=data.name,
        collected_by=data.collected_by,
        description=data.description,
        file_path=data.file_path,
        source=data.source,
        tags=data.tags
    )
    if not evidence:
        raise HTTPException(status_code=500, detail="Failed to add evidence")
    return evidence.to_dict()


@router.get("/{case_id}/evidence")
async def get_evidence(case_id: str, evidence_type: Optional[str] = None):
    """Get all evidence for case."""
    from services.case_evidence_service import CaseEvidenceService
    evidence_service = CaseEvidenceService()
    evidence_list = evidence_service.get_case_evidence(case_id, evidence_type)
    return {"evidence": [e.to_dict() for e in evidence_list]}


class ChainOfCustodyAdd(BaseModel):
    """Add chain of custody entry."""
    action: str
    user: str
    notes: Optional[str] = None


@router.post("/{case_id}/evidence/{evidence_id}/chain-of-custody")
async def add_custody_entry(case_id: str, evidence_id: int, data: ChainOfCustodyAdd):
    """Add chain of custody entry."""
    from services.case_evidence_service import CaseEvidenceService
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


@router.post("/{case_id}/iocs")
async def add_ioc(case_id: str, data: IOCAdd):
    """Add IOC to case."""
    from services.case_ioc_service import CaseIOCService
    ioc_service = CaseIOCService()
    ioc = ioc_service.add_ioc(
        case_id=case_id,
        ioc_type=data.ioc_type,
        value=data.value,
        threat_level=data.threat_level,
        confidence=data.confidence,
        source=data.source,
        tags=data.tags,
        context=data.context
    )
    if not ioc:
        raise HTTPException(status_code=500, detail="Failed to add IOC")
    return ioc.to_dict()


@router.get("/{case_id}/iocs")
async def get_iocs(case_id: str, ioc_type: Optional[str] = None):
    """Get all IOCs for case."""
    from services.case_ioc_service import CaseIOCService
    ioc_service = CaseIOCService()
    iocs = ioc_service.get_case_iocs(case_id, ioc_type)
    return {"iocs": [ioc.to_dict() for ioc in iocs]}


class IOCBulkAdd(BaseModel):
    """Bulk add IOCs."""
    iocs: List[Dict]


@router.post("/{case_id}/iocs/bulk")
async def bulk_add_iocs(case_id: str, data: IOCBulkAdd):
    """Bulk add IOCs to case."""
    from services.case_ioc_service import CaseIOCService
    ioc_service = CaseIOCService()
    count = ioc_service.bulk_add_iocs(case_id, data.iocs)
    return {"added": count}


@router.get("/{case_id}/iocs/export")
async def export_iocs(case_id: str, format: str = "json"):
    """Export IOCs (json, csv, or stix)."""
    from services.case_ioc_service import CaseIOCService
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


@router.post("/{case_id}/tasks")
async def add_task(case_id: str, data: TaskAdd):
    """Add task to case."""
    from database.connection import get_session
    from database.models import CaseTask
    
    session = get_session()
    try:
        task = CaseTask(
            case_id=case_id,
            title=data.title,
            description=data.description,
            assignee=data.assignee,
            priority=data.priority,
            status='pending',
            due_date=data.due_date,
            checklist_items=data.checklist_items or []
        )
        session.add(task)
        session.commit()
        return task.to_dict()
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to add task: {str(e)}")
    finally:
        session.close()


@router.get("/{case_id}/tasks")
async def get_tasks(case_id: str):
    """Get all tasks for case."""
    from database.connection import get_db_session
    from database.models import CaseTask
    
    try:
        session = get_db_session()
        try:
            tasks = session.query(CaseTask).filter(CaseTask.case_id == case_id).all()
            return {"tasks": [t.to_dict() for t in tasks]}
        finally:
            session.close()
    except Exception as e:
        # If database is not available, return empty list
        return {"tasks": []}


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


@router.put("/{case_id}/tasks/{task_id}")
async def update_task(case_id: str, task_id: int, data: TaskUpdate):
    """Update task."""
    from database.connection import get_session
    from database.models import CaseTask
    
    session = get_session()
    try:
        task = session.query(CaseTask).filter(CaseTask.task_id == task_id).first()
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        
        if data.title is not None:
            task.title = data.title
        if data.description is not None:
            task.description = data.description
        if data.status is not None:
            task.status = data.status
        if data.assignee is not None:
            task.assignee = data.assignee
        if data.priority is not None:
            task.priority = data.priority
        if data.due_date is not None:
            task.due_date = data.due_date
        if data.completed_at is not None:
            task.completed_at = data.completed_at
        if data.actual_hours is not None:
            task.actual_hours = data.actual_hours
        
        session.commit()
        return task.to_dict()
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to update task: {str(e)}")
    finally:
        session.close()


# Case Relationships
class RelationshipAdd(BaseModel):
    """Add case relationship."""
    related_case_id: str
    relationship_type: str
    created_by: str
    notes: Optional[str] = None


@router.post("/{case_id}/relationships")
async def add_relationship(case_id: str, data: RelationshipAdd):
    """Link related case."""
    from database.connection import get_session
    from database.models import CaseRelationship
    
    session = get_session()
    try:
        rel = CaseRelationship(
            case_id=case_id,
            related_case_id=data.related_case_id,
            relationship_type=data.relationship_type,
            created_by=data.created_by,
            notes=data.notes
        )
        session.add(rel)
        session.commit()
        return rel.to_dict()
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to add relationship: {str(e)}")
    finally:
        session.close()


@router.get("/{case_id}/relationships")
async def get_relationships(case_id: str):
    """Get related cases."""
    from database.connection import get_session
    from database.models import CaseRelationship
    
    session = get_session()
    try:
        rels = session.query(CaseRelationship).filter(
            CaseRelationship.case_id == case_id
        ).all()
        return {"relationships": [r.to_dict() for r in rels]}
    finally:
        session.close()


# Case Closure
class ClosureInfo(BaseModel):
    """Close case with metadata."""
    closure_category: str
    closed_by: str
    root_cause: Optional[str] = None
    lessons_learned: Optional[str] = None
    recommendations: Optional[str] = None
    executive_summary: Optional[str] = None


@router.post("/{case_id}/close")
async def close_case(case_id: str, data: ClosureInfo):
    """Close case with closure metadata."""
    from database.connection import get_session
    from database.models import CaseClosureInfo, Case
    
    session = get_session()
    try:
        # Update case status
        case = session.query(Case).filter(Case.case_id == case_id).first()
        if not case:
            raise HTTPException(status_code=404, detail="Case not found")
        
        case.status = 'closed'
        
        # Add closure info
        closure = CaseClosureInfo(
            case_id=case_id,
            closure_category=data.closure_category,
            closed_by=data.closed_by,
            root_cause=data.root_cause,
            lessons_learned=data.lessons_learned,
            recommendations=data.recommendations,
            executive_summary=data.executive_summary
        )
        session.add(closure)
        
        # Mark SLA resolution complete
        from services.case_sla_service import CaseSLAService
        sla_service = CaseSLAService()
        sla_service.mark_resolution_complete(case_id, session)
        
        session.commit()
        return {"success": True, "closure": closure.to_dict()}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to close case: {str(e)}")
    finally:
        session.close()


# Case Escalation
class EscalationAdd(BaseModel):
    """Escalate case."""
    escalated_from: str
    escalated_to: str
    reason: str
    urgency_level: str = "high"


@router.post("/{case_id}/escalate")
async def escalate_case(case_id: str, data: EscalationAdd):
    """Escalate case."""
    from services.case_workflow_service import CaseWorkflowService
    workflow_service = CaseWorkflowService()
    success = workflow_service.escalate_case(
        case_id, data.escalated_from, data.escalated_to, 
        data.reason, data.urgency_level
    )
    if not success:
        raise HTTPException(status_code=500, detail="Failed to escalate case")
    return {"success": True}


@router.get("/{case_id}/escalations")
async def get_escalations(case_id: str):
    """Get escalation history."""
    from database.connection import get_session
    from database.models import CaseEscalation
    
    session = get_session()
    try:
        escalations = session.query(CaseEscalation).filter(
            CaseEscalation.case_id == case_id
        ).all()
        return {"escalations": [e.to_dict() for e in escalations]}
    finally:
        session.close()


# Case Merge
class MergeRequest(BaseModel):
    """Merge another case into this one."""
    source_case_id: str
    merged_by: str = "system"


@router.post("/{case_id}/merge")
async def merge_cases(case_id: str, data: MergeRequest):
    """Merge source case into target case.

    Moves all findings, timeline entries, activities, IOCs, evidence, tasks,
    and comments from the source case into the target. The source case is
    closed with a note and linked via a 'merged_into' relationship.
    """
    if case_id == data.source_case_id:
        raise HTTPException(status_code=400, detail="Cannot merge a case into itself")

    from database.connection import get_db_manager
    from database.models import (
        Case, case_findings, CaseRelationship,
    )

    with get_db_manager().session_scope() as session:
        target = session.query(Case).filter_by(case_id=case_id).first()
        source = session.query(Case).filter_by(case_id=data.source_case_id).first()

        if not target:
            raise HTTPException(status_code=404, detail=f"Target case {case_id} not found")
        if not source:
            raise HTTPException(status_code=404, detail=f"Source case {data.source_case_id} not found")

        target_finding_ids = {f.finding_id for f in target.findings}
        moved_findings = 0
        for finding in list(source.findings):
            if finding.finding_id not in target_finding_ids:
                target.findings.append(finding)
                moved_findings += 1
            source.findings.remove(finding)

        target.timeline = (target.timeline or []) + (source.timeline or [])
        target.activities = (target.activities or []) + (source.activities or [])
        target.resolution_steps = (target.resolution_steps or []) + (source.resolution_steps or [])

        source_techniques = source.mitre_techniques or []
        target_techniques = target.mitre_techniques or []
        merged_techniques = list(set(target_techniques + source_techniques))
        target.mitre_techniques = merged_techniques

        source_tags = source.tags or []
        target_tags = target.tags or []
        target.tags = list(set(target_tags + source_tags))

        now = datetime.utcnow()
        merge_activity = {
            "timestamp": now.isoformat() + "Z",
            "activity_type": "case_merged",
            "description": f"Merged case {data.source_case_id} into this case",
            "details": {
                "source_case_id": data.source_case_id,
                "source_title": source.title,
                "findings_moved": moved_findings,
                "merged_by": data.merged_by,
            },
        }
        if not target.activities:
            target.activities = []
        target.activities.append(merge_activity)

        if target.priority and source.priority:
            priority_order = ["low", "medium", "high", "critical"]
            if priority_order.index(source.priority) > priority_order.index(target.priority):
                target.priority = source.priority

        try:
            from database.models import CaseIOC
            source_iocs = session.query(CaseIOC).filter_by(case_id=data.source_case_id).all()
            for ioc in source_iocs:
                ioc.case_id = case_id
        except Exception:
            pass

        try:
            from database.models import CaseEvidence
            source_evidence = session.query(CaseEvidence).filter_by(case_id=data.source_case_id).all()
            for ev in source_evidence:
                ev.case_id = case_id
        except Exception:
            pass

        try:
            from database.models import CaseTask
            source_tasks = session.query(CaseTask).filter_by(case_id=data.source_case_id).all()
            for task in source_tasks:
                task.case_id = case_id
        except Exception:
            pass

        try:
            from database.models import CaseComment
            source_comments = session.query(CaseComment).filter_by(case_id=data.source_case_id).all()
            for comment in source_comments:
                comment.case_id = case_id
        except Exception:
            pass

        rel = CaseRelationship(
            case_id=data.source_case_id,
            related_case_id=case_id,
            relationship_type="merged_into",
            created_by=data.merged_by,
            notes=f"Case merged into {case_id}",
        )
        session.add(rel)

        source.status = "closed"
        source.description = (source.description or "") + f"\n\n[MERGED] This case was merged into {case_id} by {data.merged_by} on {now.isoformat()}Z"

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


@router.post("/search")
async def search_cases(data: SearchRequest):
    """Advanced case search."""
    from services.case_search_service import CaseSearchService
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
        offset=data.offset
    )
    return results

