"""Findings API endpoints."""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from core.config import vigil_path
from core.findings.enrichment import (
    FindingNotFound,
    NoProviderConfigured,
    ProviderUnavailable,
    enrich,
)
from core.findings.source_evidence import (
    normalize_finding_source_evidence,
    project_finding_source_evidence_for_list,
)
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.database_data_service import DatabaseDataService

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/findings",
    tags=["findings"],
    auth=Auth.REQUIRED,
)
logger = logging.getLogger(__name__)
# Use DatabaseDataService which automatically uses PostgreSQL if available, falls back to JSON
data_service = DatabaseDataService()


@router.get("/")
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
    force_refresh: bool = Query(False),
):
    """
    Get findings with optional filters, search, and server-side pagination.

    Returns:
        Paginated list of findings with total count and has_more flag.
    """
    if force_refresh and data_service.is_s3_configured():
        logger.info("Force refresh triggered - syncing from S3")
        success, message, stats = data_service.sync_from_s3()
        if success:
            logger.info(f"S3 sync completed: {message}")
        else:
            logger.warning(f"S3 sync failed or partial: {message}")

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
        # The list view never uses the 768-float embedding — omit it so a
        # 1000-row page (polled every 10s) isn't several MB of vectors.
        include_embedding=False,
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


@router.get("/stats/summary")
def get_findings_summary():
    """
    Get summary statistics for findings.

    Returns:
        Summary statistics
    """
    findings = data_service.get_findings(include_embedding=False)

    # Calculate statistics
    severity_counts = {}
    data_source_counts = {}
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


@router.post("/export")
def export_findings(output_format: str = "json"):
    from datetime import datetime

    output_dir = vigil_path("exports", write=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"findings_export_{timestamp}.{output_format}"

    success = data_service.export_findings(output_path, format=output_format)

    if success:
        return {"success": True, "file_path": str(output_path)}
    else:
        raise HTTPException(status_code=500, detail="Export failed")


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


class BulkEnrichmentRequest(BaseModel):
    """Schema for bulk enrichment request."""

    finding_ids: List[str]
    enrichment_data: Dict[str, FindingUpdate]


@router.patch("/{finding_id}")
def update_finding(finding_id: str, update: FindingUpdate):
    """
    Update/enrich an existing finding.

    This endpoint allows you to add or update information on a finding,
    including MITRE ATT&CK technique mappings, severity, and other metadata.

    Args:
        finding_id: The finding ID to update
        update: Fields to update

    Returns:
        Updated finding

    Example:
        PATCH /api/findings/f-20260114-abc123
        {
            "mitre_predictions": {"T1071.001": 0.85, "T1048.003": 0.72},
            "predicted_techniques": [
                {"technique_id": "T1071.001", "confidence": 0.85},
                {"technique_id": "T1048.003", "confidence": 0.72}
            ],
            "severity": "high"
        }
    """
    # Get existing finding
    finding = data_service.get_finding(finding_id)
    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")

    # Prepare updates (exclude None values)
    updates = {}
    for key, value in update.model_dump(exclude_none=True).items():
        updates[key] = value

    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")

    # Update the finding
    success = data_service.update_finding(finding_id, **updates)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to update finding")

    # Return updated finding
    updated_finding = data_service.get_finding(finding_id)
    logger.info(f"Updated finding {finding_id} with {len(updates)} fields")

    return {
        "success": True,
        "finding": updated_finding,
        "updated_fields": list(updates.keys()),
    }


@router.post("/bulk-enrich")
def bulk_enrich_findings(request: BulkEnrichmentRequest):
    """
    Bulk enrich multiple findings with MITRE ATT&CK and other data.

    This endpoint allows you to enrich multiple findings at once,
    useful for batch processing or adding threat intelligence data.

    Args:
        request: Bulk enrichment request with finding IDs and enrichment data

    Returns:
        Summary of enrichment results

    Example:
        POST /api/findings/bulk-enrich
        {
            "finding_ids": ["f-001", "f-002"],
            "enrichment_data": {
                "f-001": {
                    "mitre_predictions": {"T1071.001": 0.85},
                    "severity": "high"
                },
                "f-002": {
                    "mitre_predictions": {"T1059.001": 0.92},
                    "severity": "critical"
                }
            }
        }
    """
    results = {
        "total": len(request.finding_ids),
        "updated": 0,
        "failed": 0,
        "not_found": 0,
        "errors": [],
    }

    for finding_id in request.finding_ids:
        try:
            # Check if finding exists
            finding = data_service.get_finding(finding_id)
            if not finding:
                results["not_found"] += 1
                results["errors"].append(f"{finding_id}: Not found")
                continue

            # Get enrichment data for this finding
            enrichment = request.enrichment_data.get(finding_id)
            if not enrichment:
                continue

            # Prepare updates
            updates = enrichment.model_dump(exclude_none=True)
            if not updates:
                continue

            # Update the finding
            success = data_service.update_finding(finding_id, **updates)

            if success:
                results["updated"] += 1
                logger.info(f"Enriched finding {finding_id}")
            else:
                results["failed"] += 1
                results["errors"].append(f"{finding_id}: Update failed")

        except Exception as e:
            results["failed"] += 1
            results["errors"].append(f"{finding_id}: {str(e)}")
            logger.error(f"Error enriching finding {finding_id}: {e}")

    return {
        "success": results["updated"] > 0,
        "message": f"Updated {results['updated']} of {results['total']} findings",
        "results": results,
    }


@router.post("/{finding_id}/enrich")
async def get_or_generate_enrichment(
    finding_id: str, force_regenerate: bool = Query(False)
):
    """
    Get or generate AI enrichment for a finding.

    This endpoint checks if AI enrichment already exists for the finding.
    If it exists, returns the cached enrichment immediately.
    If not, generates new enrichment using the configured reporting model,
    caches it, and returns it.

    Args:
        finding_id: The finding ID to enrich
        force_regenerate: Force regeneration even if enrichment exists

    Returns:
        AI enrichment data with threat analysis, impact, recommendations, etc.

    Example Response:
        {
            "finding_id": "f-20260114-001",
            "cached": false,
            "enrichment": {
                "threat_summary": "...",
                "potential_impact": "...",
                "recommended_actions": [...],
                "related_techniques": [...],
                "indicators": {...},
                "confidence_score": 0.85
            }
        }
    """
    import asyncio

    # Get the finding. The data layer is synchronous SQLAlchemy, so keep it off
    # the event loop — this handler stays async because it awaits the LLM.
    finding = await asyncio.to_thread(data_service.get_finding, finding_id)
    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")

    # Check if enrichment already exists. Caching is HTTP policy — the shared
    # enrich() seam deliberately doesn't do this check, since `force_regenerate`
    # is a query param and the daemon has its own freshness rules.
    existing_enrichment = finding.get("ai_enrichment")
    if existing_enrichment and not force_regenerate:
        logger.info(f"Returning cached enrichment for {finding_id}")
        return {
            "finding_id": finding_id,
            "cached": True,
            "enrichment": existing_enrichment,
        }

    # Generate new enrichment using the configured reporting provider. The flow
    # lives in services/findings/enrichment/ so ingestion, the daemon and agents
    # can reuse it; this handler owns only the domain-error → status-code
    # translation. The write that used to sit here moved into that module's
    # _persist(), which keeps the to_thread offload.
    try:
        # Pass the path param, not the id off the row we just read — it's the
        # authoritative write target, exactly as the pre-extraction handler did.
        enrichment = await enrich(
            finding, finding_id=finding_id, data_service=data_service
        )
    except FindingNotFound:
        raise HTTPException(status_code=404, detail="Finding not found")
    except NoProviderConfigured:
        # 503 with the structured payload the chat drawer matches on to render
        # a "Configure a provider" CTA instead of a generic error bubble.
        from services.api.routers.claude import NO_PROVIDER_DETAIL

        raise HTTPException(status_code=503, detail=NO_PROVIDER_DETAIL)
    except ProviderUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Error generating enrichment for {finding_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to generate enrichment: {str(e)}"
        )

    return {"finding_id": finding_id, "cached": False, "enrichment": enrichment}


@router.delete("/all")
def clear_all_findings(session: UnitOfWorkSession):
    """Delete all findings from the database."""
    try:
        from core.storage.models import Finding

        count = session.query(Finding).count()
        session.query(Finding).delete()

        logger.info(f"Cleared {count} findings")
        return {
            "success": True,
            "deleted": count,
            "message": f"Deleted {count} findings",
        }
    except Exception as e:
        logger.error(f"Error clearing findings: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to clear findings: {str(e)}"
        )
