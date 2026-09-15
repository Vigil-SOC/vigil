"""Findings — versioned contract surface (``/api/v1/findings``).

The read and record-update routes an external consumer relies on: list, get,
summary, export, and the enrich-metadata PATCH. Enrichment *generation*
(``/bulk-enrich``, ``/{id}/enrich``) and the destructive ``/all`` wipe stay on
the unversioned router in ``services/api/routers/findings.py`` — they are
operator/console actions, not part of the frozen surface.

``FindingUpdate`` is defined here because it is the update contract; the
unversioned router imports it back for its bulk endpoint (services -> core,
the allowed direction).
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from core.config import vigil_path
from core.findings.source_evidence import (
    normalize_finding_source_evidence,
    project_finding_source_evidence_for_list,
)
from core.routing import Auth, RouterMeta
from core.storage.database_data_service import DatabaseDataService

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/v1/findings",
    tags=["findings"],
    auth=Auth.REQUIRED,
    legacy_prefixes=("/api/findings",),
)
logger = logging.getLogger(__name__)
data_service = DatabaseDataService()


class FindingUpdate(BaseModel):
    """Schema for updating a finding."""

    mitre_predictions: Optional[Dict[str, float]] = None
    predicted_techniques: Optional[List[Dict[str, Any]]] = None
    severity: Optional[str] = None
    status: Optional[str] = None
    anomaly_score: Optional[float] = None
    entity_context: Optional[Dict[str, Any]] = None
    cluster_id: Optional[str] = None
    evidence_links: Optional[List[str]] = None




class FindingListResponse(BaseModel):
    findings: List[Dict[str, Any]] = Field(default_factory=list)
    total: int
    offset: int
    limit: int
    has_more: bool


class FindingsSummaryResponse(BaseModel):
    total: int
    by_severity: Dict[str, int] = Field(default_factory=dict)
    by_data_source: Dict[str, int] = Field(default_factory=dict)


class FindingExportResponse(BaseModel):
    success: bool
    file_path: str


class FindingUpdateResponse(BaseModel):
    success: bool
    finding: Dict[str, Any] = Field(default_factory=dict)
    updated_fields: List[str] = Field(default_factory=list)


@router.get("/", response_model=FindingListResponse)
def get_findings(
    severity: Optional[str] = Query(None),
    data_source: Optional[str] = Query(None),
    cluster_id: Optional[int] = Query(None),
    min_anomaly_score: Optional[float] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(
        None, description="Text search across finding IDs, descriptions, entity context"
    ),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    sort_by: str = Query("timestamp"),
    sort_order: str = Query("desc"),
):
    """
    Get findings with optional filters, search, and server-side pagination.

    Returns:
        Paginated list of findings with total count and has_more flag.
    """
    cluster_id_str = str(cluster_id) if cluster_id is not None else None

    total = data_service.count_findings(
        severity=severity,
        data_source=data_source,
        cluster_id=cluster_id_str,
        min_anomaly_score=min_anomaly_score,
        status=status,
        search_query=search,
    )
    findings = data_service.get_findings(
        limit=limit,
        offset=offset,
        severity=severity,
        data_source=data_source,
        cluster_id=cluster_id_str,
        min_anomaly_score=min_anomaly_score,
        status=status,
        search_query=search,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    return {
        "findings": [
            project_finding_source_evidence_for_list(finding) for finding in findings
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": (offset + limit) < total,
    }


# No response_model: returns the normalized finding record verbatim. Its
# shape is owned by FindingSchema (with ORM-parity tests); forcing a model
# here would strip the normalized source_evidence. Snapshot pins the op.
@router.get("/{finding_id}")
def get_finding(finding_id: str):
    """
    Get a specific finding by ID.

    Args:
        finding_id: The finding ID

    Returns:
        Finding details
    """
    finding = data_service.get_finding(finding_id)
    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")
    return normalize_finding_source_evidence(finding)


@router.get("/stats/summary", response_model=FindingsSummaryResponse)
def get_findings_summary():
    """
    Get summary statistics for findings.

    Returns:
        Summary statistics
    """
    findings = data_service.get_findings()

    severity_counts: Dict[str, int] = {}
    data_source_counts: Dict[str, int] = {}
    total_count = len(findings)

    for finding in findings:
        severity = finding.get("severity", "unknown")
        severity_counts[severity] = severity_counts.get(severity, 0) + 1

        data_source = finding.get("data_source", "unknown")
        data_source_counts[data_source] = data_source_counts.get(data_source, 0) + 1

    return {
        "total": total_count,
        "by_severity": severity_counts,
        "by_data_source": data_source_counts,
    }


@router.post("/export", response_model=FindingExportResponse)
def export_findings(output_format: str = "json"):
    output_dir = vigil_path("exports", write=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"findings_export_{timestamp}.{output_format}"

    success = data_service.export_findings(output_path, format=output_format)

    if success:
        return {"success": True, "file_path": str(output_path)}
    raise HTTPException(status_code=500, detail="Export failed")


@router.patch("/{finding_id}", response_model=FindingUpdateResponse)
def update_finding(finding_id: str, update: FindingUpdate):
    """
    Update/enrich an existing finding.

    Add or update information on a finding, including MITRE ATT&CK technique
    mappings, severity, and other metadata.

    Args:
        finding_id: The finding ID to update
        update: Fields to update

    Returns:
        Updated finding
    """
    finding = data_service.get_finding(finding_id)
    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")

    updates = {}
    for key, value in update.model_dump(exclude_none=True).items():
        updates[key] = value

    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")

    success = data_service.update_finding(finding_id, **updates)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to update finding")

    updated_finding = data_service.get_finding(finding_id)
    logger.info(f"Updated finding {finding_id} with {len(updates)} fields")

    return {
        "success": True,
        "finding": updated_finding,
        "updated_fields": list(updates.keys()),
    }
