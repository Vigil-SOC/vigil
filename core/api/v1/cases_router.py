"""Cases — versioned contract surface (``/api/v1/cases``).

The case record and its lifecycle: list, get, create, update, close, merge,
search, the case summary, finding links, evidence (chain of custody), and IOCs.
These are the durable record an external caller or the platform reads and
writes.

Everything console-flavoured or unsettled stays on the unversioned router in
``services/api/routers/cases.py`` at ``/api/cases``: comments, watchers, tasks,
activities, resolution steps, SLA, escalation, relationships, per-case report
generation, and the destructive ``DELETE /all`` and ``DELETE /{id}`` wipes.

Request schemas for the contract routes are defined here (they are part of the
contract); the console router does not use them.
"""

from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth.current_user import get_current_user
from core.cases.case_evidence_service import CaseEvidenceService
from core.cases.case_ioc_service import CaseIOCService
from core.cases.closure import ClosedByKind, ClosureCategory
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.database_data_service import DatabaseDataService
from core.storage.models import User
from core.storage.schemas import (
    CaseClosureInfoSchema,
    CaseEvidenceSchema,
    CaseIOCSchema,
    CaseSchema,
)
from core.storage.schemas.case_api import (
    CaseCloseResponse,
    CaseEvidenceListResponse,
    CaseIOCBulkResponse,
    CaseIOCExportResponse,
    CaseIOCListResponse,
    CaseListResponse,
    CaseMergeResponse,
    CaseSearchResponse,
    CaseSuccessResponse,
    CaseSummaryResponse,
)
from core.time import utcnow

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/v1/cases",
    tags=["cases"],
    auth=Auth.REQUIRED,
    legacy_prefixes=("/api/cases",),
)

data_service = DatabaseDataService()


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


class EvidenceAdd(BaseModel):
    """Add evidence to case."""

    evidence_type: str
    name: str
    collected_by: str
    description: Optional[str] = None
    file_path: Optional[str] = None
    source: Optional[str] = None
    tags: Optional[List[str]] = None


class IOCAdd(BaseModel):
    """Add IOC to case."""

    ioc_type: str
    value: str
    threat_level: Optional[str] = None
    confidence: Optional[float] = None
    source: Optional[str] = None
    tags: Optional[List[str]] = None
    context: Optional[str] = None


class IOCBulkAdd(BaseModel):
    """Bulk add IOCs."""

    iocs: List[Dict]


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


class MergeRequest(BaseModel):
    """Merge another case into this one."""

    source_case_id: str
    merged_by: str = "system"


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
    case = data_service.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    finding_ids = case.get("finding_ids", [])
    if finding_id not in finding_ids:
        finding_ids.append(finding_id)
        success = data_service.update_case(case_id, finding_ids=finding_ids)
        if not success:
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
    case = data_service.get_case(case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    finding_ids = case.get("finding_ids", [])
    if finding_id in finding_ids:
        finding_ids.remove(finding_id)
        success = data_service.update_case(case_id, finding_ids=finding_ids)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to remove finding")

    return data_service.get_case(case_id)


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
