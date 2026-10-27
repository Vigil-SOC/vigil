"""ATT&CK framework API endpoints."""

from typing import Annotated, Optional

from fastapi import APIRouter, Query

from core.detections.tools import get_security_detection_tools
from core.routing import Auth, RouterMeta
from core.storage.database_data_service import DatabaseDataService
from core.threat_intel.mitre_lookup import (
    get_time_range,
    iter_techniques,
    meets_threshold,
    rank_by_confidence,
    resolve_technique,
)
from core.threat_intel.occurrence_rollup import findings_in_window
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

    min_confidence applies only to techniques with a confidence: at 0 an
    occurrence with no confidence is counted, above 0 it is excluded and counted
    in excluded_occurrences on the response.

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
def get_findings_by_technique(
    technique_id: str,
    min_confidence: float = 0.0,
    # Annotated so a direct call (tests, tools) gets "all", not the Query object.
    time_range: Annotated[str, Query(pattern="^(24h|7d|30d|all)$")] = "all",
):
    """
    Get the findings associated with a specific technique.

    Args:
        technique_id: MITRE ATT&CK technique ID
        min_confidence: The rollup's threshold, applied to this technique.
        time_range: The rollup's window — '24h', '7d', '30d', or 'all' (default).

    Returns:
        Findings with a confidence for the technique, highest first, then those
        the source named it on without one.
    """
    # Hidden and filtered like the rollup it drills into, so its count and this
    # list agree.
    if data_service.is_using_database():
        start_time = end_time = None
        if time_range != "all":
            start_time, end_time = get_time_range(time_range)
        matching_findings = data_service.get_findings_by_technique(
            technique_id,
            exclusions="hide",
            min_confidence=min_confidence,
            start_time=start_time,
            end_time=end_time,
        )
    else:
        matches = []
        for finding in findings_in_window(
            data_service.get_findings(exclusions="hide"), time_range
        ):
            for tech in iter_techniques(finding):
                if (tech.get("technique_id") or tech.get("id")) != technique_id:
                    continue
                if meets_threshold(tech.get("confidence"), min_confidence):
                    matches.append((finding, tech.get("confidence")))
                break
        matching_findings = [
            f for f, _ in rank_by_confidence(matches, confidence=lambda m: m[1])
        ]

    return {
        "technique_id": technique_id,
        "findings": matching_findings,
        "total": len(matching_findings),
    }
