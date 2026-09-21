"""Findings — unversioned operator/console endpoints.

The frozen read and record-update surface lives in ``core/api/v1/findings_router.py``
and is mounted at both ``/api/v1/findings`` and (for now) ``/api/findings``.
This module keeps the routes that are *not* part of that contract: AI
enrichment generation and the destructive wipe. They remain under
``/api/findings`` only.

``FindingUpdate`` is the contract schema, imported from the v1 module
(services -> core, the allowed direction).
"""

import logging
from typing import Dict, List

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from core.api.v1.findings_router import FindingUpdate
from core.config import vigil_path
from core.findings.enrichment import (
    FindingNotFound,
    NoProviderConfigured,
    ProviderUnavailable,
    enrich,
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
data_service = DatabaseDataService()


class BulkEnrichmentRequest(BaseModel):
    """Schema for bulk enrichment request."""

    finding_ids: List[str]
    enrichment_data: Dict[str, FindingUpdate]


@router.post("/bulk-enrich")
def bulk_enrich_findings(request: BulkEnrichmentRequest):
    """
    Bulk enrich multiple findings with MITRE ATT&CK and other data.

    Args:
        request: Bulk enrichment request with finding IDs and enrichment data

    Returns:
        Summary of enrichment results
    """
    results: Dict[str, object] = {
        "total": len(request.finding_ids),
        "updated": 0,
        "failed": 0,
        "not_found": 0,
        "errors": [],
    }

    for finding_id in request.finding_ids:
        try:
            finding = data_service.get_finding(finding_id)
            if not finding:
                results["not_found"] += 1
                results["errors"].append(f"{finding_id}: Not found")
                continue

            enrichment = request.enrichment_data.get(finding_id)
            if not enrichment:
                continue

            updates = enrichment.model_dump(exclude_none=True)
            if not updates:
                continue

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

    Returns cached enrichment if present, otherwise generates, caches, and
    returns new enrichment from the configured reporting model.

    Args:
        finding_id: The finding ID to enrich
        force_regenerate: Force regeneration even if enrichment exists

    Returns:
        AI enrichment data with threat analysis, impact, recommendations, etc.
    """
    import asyncio

    # The data layer is synchronous SQLAlchemy, so keep it off the event loop —
    # this handler stays async because it awaits the LLM.
    finding = await asyncio.to_thread(data_service.get_finding, finding_id)
    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")

    # Caching is HTTP policy — the shared enrich() seam deliberately doesn't do
    # this check, since force_regenerate is a query param and the daemon has its
    # own freshness rules.
    existing_enrichment = finding.get("ai_enrichment")
    if existing_enrichment and not force_regenerate:
        logger.info(f"Returning cached enrichment for {finding_id}")
        return {
            "finding_id": finding_id,
            "cached": True,
            "enrichment": existing_enrichment,
        }

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

    return {"finding_id": finding_id, "cached": False, "enrichment": enrichment}


@router.delete("/all")
def clear_all_findings(session: UnitOfWorkSession):
    """Delete all findings from the database."""
    from core.storage.models import Finding

    count = session.query(Finding).count()
    session.query(Finding).delete()

    logger.info(f"Cleared {count} findings")
    return {"success": True, "deleted": count, "message": f"Deleted {count} findings"}


# Not in the frozen surface: this writes a file on the server and answers with
# its path, which is nothing an external caller can open. It stays unversioned
# until it answers with the export itself.
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
