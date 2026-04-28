"""ATT&CK framework API endpoints."""

from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Query
import logging

from services.database_data_service import DatabaseDataService
from services.mitre_lookup import iter_techniques, resolve_technique
from backend.api.analytics import get_time_range

router = APIRouter()
logger = logging.getLogger(__name__)
data_service = DatabaseDataService()


def _parse_finding_timestamp(finding: dict) -> Optional[datetime]:
    """Parse a finding's timestamp (ISO string or datetime) into a naive UTC datetime.

    Findings come from `Finding.to_dict()` (ISO string) or the JSON fallback
    (raw dict values) — handle both.
    """
    raw = finding.get("timestamp") or finding.get("created_at")
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None) if raw.tzinfo else raw
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
        except ValueError:
            return None
    return None


@router.get("/layer")
async def get_attack_layer():
    """
    Get ATT&CK Navigator layer data.

    Builds an ATT&CK Navigator layer from findings technique predictions.

    Returns:
        ATT&CK layer JSON
    """
    try:
        findings = data_service.get_findings()

        # Build technique scores from findings
        technique_scores = {}
        for finding in findings:
            for tech in iter_techniques(finding):
                tid = tech.get("technique_id") or tech.get("id")
                confidence = tech.get("confidence", 0) or 0
                if tid:
                    # Track max confidence per technique
                    technique_scores[tid] = max(
                        technique_scores.get(tid, 0), confidence
                    )

        techniques = [
            {
                "techniqueID": tid,
                "score": round(score * 100),
                "color": "",
                "comment": "",
                "enabled": True,
            }
            for tid, score in technique_scores.items()
        ]

        layer = {
            "name": "DeepTempo Findings",
            "version": "4.5",
            "domain": "enterprise-attack",
            "description": "ATT&CK techniques detected in findings",
            "techniques": techniques,
        }

        return layer
    except Exception as e:
        logger.error(f"Error building ATT&CK layer: {e}")
        return {
            "name": "DeepTempo Findings",
            "version": "4.5",
            "domain": "enterprise-attack",
            "description": "ATT&CK techniques detected in findings",
            "techniques": [],
        }


@router.get("/techniques/rollup")
async def get_technique_rollup(
    min_confidence: float = 0.0,
    time_range: str = Query("all", regex="^(24h|7d|30d|all)$"),
):
    """
    Get rollup of ATT&CK techniques across all findings.

    Args:
        min_confidence: Minimum confidence threshold
        time_range: Optional time window — '24h', '7d', '30d', or 'all' (default).

    Returns:
        Technique statistics sorted by occurrence count, including
        human-readable technique name and tactic per row.
    """
    findings = data_service.get_findings()

    if time_range != "all":
        start_time, end_time = get_time_range(time_range)
        scoped: list[dict] = []
        for finding in findings:
            ts = _parse_finding_timestamp(finding)
            if ts is None or start_time <= ts <= end_time:
                scoped.append(finding)
        findings = scoped

    technique_counts: dict[str, int] = {}
    technique_severities: dict[str, dict[str, int]] = {}
    technique_meta: dict[str, tuple[str, str]] = {}

    for finding in findings:
        severity = finding.get("severity", "unknown")

        for tech in iter_techniques(finding):
            confidence = tech.get("confidence", 0) or 0

            if confidence < min_confidence:
                continue

            tid, name, tactic = resolve_technique(tech)
            if not tid:
                continue

            technique_counts[tid] = technique_counts.get(tid, 0) + 1
            if tid not in technique_meta:
                technique_meta[tid] = (name, tactic)

            if tid not in technique_severities:
                technique_severities[tid] = {
                    "critical": 0,
                    "high": 0,
                    "medium": 0,
                    "low": 0,
                }

            technique_severities[tid][severity] = (
                technique_severities[tid].get(severity, 0) + 1
            )

    techniques = []
    for tid, count in technique_counts.items():
        name, tactic = technique_meta[tid]
        techniques.append(
            {
                "technique_id": tid,
                "technique_name": name,
                "tactic": tactic,
                "count": count,
                "severities": technique_severities[tid],
            }
        )

    techniques.sort(key=lambda x: x["count"], reverse=True)

    return {
        "total_techniques": len(techniques),
        "techniques": techniques,
    }


@router.get("/techniques/{technique_id}/findings")
async def get_findings_by_technique(technique_id: str):
    """
    Get all findings associated with a specific technique.

    Args:
        technique_id: MITRE ATT&CK technique ID

    Returns:
        List of findings
    """
    findings = data_service.get_findings()

    matching_findings = []

    for finding in findings:
        for tech in iter_techniques(finding):
            if (tech.get("technique_id") or tech.get("id")) == technique_id:
                matching_findings.append(finding)
                break

    return {
        "technique_id": technique_id,
        "findings": matching_findings,
        "total": len(matching_findings),
    }


@router.get("/tactics/summary")
async def get_tactics_summary():
    """
    Get summary of tactics across all findings.

    Returns:
        Tactics summary
    """
    findings = data_service.get_findings()

    tactic_counts: dict[str, int] = {}

    for finding in findings:
        for tech in iter_techniques(finding):
            _tid, _name, tactic = resolve_technique(tech)
            tactic_counts[tactic] = tactic_counts.get(tactic, 0) + 1

    return {
        "tactics": [
            {"tactic": tactic, "count": count}
            for tactic, count in sorted(
                tactic_counts.items(),
                key=lambda x: x[1],
                reverse=True,
            )
        ]
    }
