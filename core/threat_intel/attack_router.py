"""ATT&CK framework API endpoints."""

from typing import Optional

from fastapi import APIRouter, Query

from core.detections.tools import get_security_detection_tools
from core.routing import Auth, RouterMeta
from core.storage.database_data_service import DatabaseDataService
from core.threat_intel.mitre_lookup import iter_techniques, resolve_technique
from core.threat_intel.occurrence_rollup import occurrence_rollup as _occurrence_rollup

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/attack",
    tags=["attack"],
    auth=Auth.REQUIRED,
)
data_service = DatabaseDataService()


def occurrence_rollup(min_confidence: float = 0.0, time_range: str = "all"):
    """Counts for this router's data service, so a test can substitute it."""
    return _occurrence_rollup(
        min_confidence=min_confidence,
        time_range=time_range,
        service=data_service,
    )


@router.get("/techniques/rollup")
async def get_technique_rollup(
    min_confidence: float = 0.0,
    time_range: str = Query("all", pattern="^(24h|7d|30d|all)$"),
    run_id: Optional[str] = None,
):
    """
    Get rollup of ATT&CK techniques across all findings, or a run coverage report.

    Args:
        min_confidence: Minimum confidence threshold (occurrence rollup only).
        time_range: Optional time window — '24h', '7d', '30d', or 'all' (default).
        run_id: Optional agent run id. When present, reconstruct that run on read
            and return per-technique verdicts; occurrence counts are unchanged
            when omitted.

    Returns:
        Technique statistics sorted by occurrence count, or coverage rows with
        layer verdicts and missed-step evidence when a run is selected.
    """
    if run_id:
        return await _coverage_rollup(run_id)
    return occurrence_rollup(min_confidence=min_confidence, time_range=time_range)


async def _coverage_rollup(run_id: str) -> dict:
    report = await get_security_detection_tools().analyze_coverage(run_id=run_id)
    techniques = []
    for row in report.get("techniques") or []:
        tid = row.get("technique_id") or ""
        resolved_id, name, tactic = resolve_technique(tid)
        techniques.append(
            {
                **row,
                "technique_id": resolved_id or tid,
                "technique_name": name,
                "tactic": tactic,
            }
        )
    body: dict = {
        "run_id": run_id,
        "total_techniques": len(techniques),
        "techniques": techniques,
    }
    if report.get("error"):
        body["error"] = report["error"]
    return body


@router.get("/techniques/{technique_id}/findings")
def get_findings_by_technique(technique_id: str):
    """
    Get all findings associated with a specific technique.

    Args:
        technique_id: MITRE ATT&CK technique ID

    Returns:
        List of findings
    """
    # Hidden like the rollup it drills into, so its count and this list agree.
    if data_service.is_using_database():
        matching_findings = data_service.get_findings_by_technique(
            technique_id, exclusions="hide"
        )
    else:
        matching_findings = []
        for finding in data_service.get_findings(exclusions="hide"):
            for tech in iter_techniques(finding):
                if (tech.get("technique_id") or tech.get("id")) == technique_id:
                    matching_findings.append(finding)
                    break

    return {
        "technique_id": technique_id,
        "findings": matching_findings,
        "total": len(matching_findings),
    }
