"""Findings — versioned contract surface (``/api/v1/findings``).

The read and record-update routes an external consumer relies on: list, get,
summary, and the enrich-metadata PATCH. Enrichment *generation*
(``/bulk-enrich``, ``/{id}/enrich``), the destructive ``/all`` wipe and
``/export`` stay on the unversioned router in
``services/api/routers/findings.py`` — they are operator/console actions, not
part of the frozen surface. ``/export`` in particular writes a file on the
server and answers with its path, which is nothing an external caller can open;
freezing that shape would promise it for the life of 1.x.

``FindingUpdate`` is defined here because it is the update contract; the
unversioned router imports it back for its bulk endpoint (services -> core,
the allowed direction).
"""

import logging
from collections.abc import Mapping
from typing import Annotated, Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    ValidatorFunctionWrapHandler,
    WrapSerializer,
    WrapValidator,
)

from core.findings.exclusions import current_active_ips, excluded_ips_of
from core.findings.source_evidence import (
    StoredSourceEvidence,
    normalize_finding_source_evidence,
    project_finding_source_evidence_for_list,
)
from core.routing import Auth, RouterMeta
from core.storage.database_data_service import DatabaseDataService
from core.storage.schemas.finding import FindingSchema

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/v1/findings",
    tags=["findings"],
    auth=Auth.REQUIRED,
    legacy_prefixes=("/api/findings",),
)
logger = logging.getLogger(__name__)
data_service = DatabaseDataService()


ExclusionView = Literal["include", "hide", "only"]

_EXCLUSIONS_QUERY = Query(
    "include",
    description=(
        "Findings naming an analyst-excluded IP: include them (default), hide "
        "them, or return only them. Each finding carries `excluded_ips`."
    ),
)
_SUMMARY_EXCLUSIONS_QUERY = Query(
    "include",
    description=(
        "Count findings naming an analyst-excluded IP (include, the default), "
        "leave them out (hide), or count only them (only)."
    ),
)


def _annotate_exclusions(finding: Dict[str, Any], active) -> Dict[str, Any]:
    finding["excluded_ips"] = excluded_ips_of(finding, active)
    return finding


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


class EntityContext(BaseModel):
    """Free-form entity context; ``source_evidence`` is the one named key."""

    model_config = ConfigDict(extra="allow")

    source_evidence: Optional[StoredSourceEvidence] = None


# A non-object entity_context stored in JSONB passes through untyped rather
# than failing the response; the published schema stays EntityContext | null.
def _validate_context(value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
    return handler(value) if value is None or isinstance(value, Mapping) else value


# No return annotation: pydantic would publish it as the serialized schema.
def _serialize_context(value: Any, handler: SerializerFunctionWrapHandler):
    return (
        handler(value) if value is None or isinstance(value, EntityContext) else value
    )


TolerantEntityContext = Annotated[
    Optional[EntityContext],
    WrapValidator(_validate_context),
    WrapSerializer(_serialize_context),
]


class FindingRecord(FindingSchema):
    """A finding as the API returns it.

    ``FindingSchema`` stays in the storage tier, which may not import the
    findings domain, so the evidence type is narrowed here.
    """

    entity_context: TolerantEntityContext = None
    excluded_ips: List[str] = Field(default_factory=list)


class FindingListResponse(BaseModel):
    findings: List[FindingRecord] = Field(default_factory=list)
    total: int
    offset: int
    limit: int
    has_more: bool


class FindingsSummaryResponse(BaseModel):
    total: int
    by_severity: Dict[str, int] = Field(default_factory=dict)
    by_data_source: Dict[str, int] = Field(default_factory=dict)


class FindingUpdateResponse(BaseModel):
    success: bool
    finding: Dict[str, Any] = Field(default_factory=dict)
    updated_fields: List[str] = Field(default_factory=list)


# exclude_unset: an envelope's absent payload keys must stay absent, not null.
@router.get("", response_model=FindingListResponse, response_model_exclude_unset=True)
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
    exclusions: ExclusionView = _EXCLUSIONS_QUERY,
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
        exclusions=exclusions,
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
        exclusions=exclusions,
    )
    active = current_active_ips()

    return {
        "findings": [
            _annotate_exclusions(
                project_finding_source_evidence_for_list(finding), active
            )
            for finding in findings
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": (offset + limit) < total,
    }


@router.get(
    "/{finding_id}", response_model=FindingRecord, response_model_exclude_unset=True
)
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
    return _annotate_exclusions(
        normalize_finding_source_evidence(finding), current_active_ips()
    )


@router.get("/stats/summary", response_model=FindingsSummaryResponse)
def get_findings_summary(exclusions: ExclusionView = _SUMMARY_EXCLUSIONS_QUERY):
    """
    Get summary statistics for findings.

    Returns:
        Summary statistics
    """
    findings = data_service.get_findings(exclusions=exclusions)

    severity_counts: Dict[str, int] = {}
    data_source_counts: Dict[str, int] = {}
    total_count = len(findings)

    for finding in findings:
        # `or`, not a get default: LogLM parquet ingest stores a null severity.
        severity = finding.get("severity") or "unknown"
        severity_counts[severity] = severity_counts.get(severity, 0) + 1

        data_source = finding.get("data_source") or "unknown"
        data_source_counts[data_source] = data_source_counts.get(data_source, 0) + 1

    return {
        "total": total_count,
        "by_severity": severity_counts,
        "by_data_source": data_source_counts,
    }


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
