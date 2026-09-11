"""ATT&CK framework API endpoints."""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Query

from core.detections.tools import get_security_detection_tools
from core.routing import Auth, RouterMeta
from core.storage.database_data_service import DatabaseDataService
from core.threat_intel.mitre_lookup import (
    get_time_range,
    iter_techniques,
    resolve_technique,
)

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/attack",
    tags=["attack"],
    auth=Auth.REQUIRED,
)
logger = logging.getLogger(__name__)
data_service = DatabaseDataService()


def _parse_finding_timestamp(finding: dict) -> Optional[datetime]:
    """Parse a finding's timestamp (ISO string or datetime) into a naive UTC datetime.

    Findings come from `Finding.to_dict()` (ISO string) or dumped dict values.
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
def get_attack_layer():
    """
    Get ATT&CK Navigator layer data.

    Builds an ATT&CK Navigator layer from findings technique predictions.

    Returns:
        ATT&CK layer JSON
    """
    try:
        if data_service.is_using_database():
            technique_scores = data_service.get_technique_max_confidence()
        else:
            findings = data_service.get_findings()
            technique_scores = {}
            for finding in findings:
                for tech in iter_techniques(finding):
                    tid = tech.get("technique_id") or tech.get("id")
                    confidence = tech.get("confidence", 0) or 0
                    if tid:
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


def occurrence_rollup(
    min_confidence: float = 0.0,
    time_range: str = "all",
):
    if data_service.is_using_database():
        start_time = end_time = None
        if time_range != "all":
            start_time, end_time = get_time_range(time_range)
        severity_rows = data_service.get_technique_severity_counts(
            min_confidence=min_confidence,
            start_time=start_time,
            end_time=end_time,
        )
        technique_counts: dict[str, int] = {}
        technique_severities: dict[str, dict[str, int]] = {}
        technique_meta: dict[str, tuple[str, str]] = {}
        for tid, severity, count in severity_rows:
            resolved_id, name, tactic = resolve_technique(tid)
            if not resolved_id:
                continue
            technique_counts[resolved_id] = technique_counts.get(resolved_id, 0) + count
            if resolved_id not in technique_meta:
                technique_meta[resolved_id] = (name, tactic)
            if resolved_id not in technique_severities:
                technique_severities[resolved_id] = {
                    "critical": 0,
                    "high": 0,
                    "medium": 0,
                    "low": 0,
                }
            sev_key = severity or "unknown"
            technique_severities[resolved_id][sev_key] = (
                technique_severities[resolved_id].get(sev_key, 0) + count
            )
    else:
        findings = data_service.get_findings()

        if time_range != "all":
            start_time, end_time = get_time_range(time_range)
            scoped: list[dict] = []
            for finding in findings:
                ts = _parse_finding_timestamp(finding)
                if ts is None or start_time <= ts <= end_time:
                    scoped.append(finding)
            findings = scoped

        technique_counts = {}
        technique_severities = {}
        technique_meta = {}

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
def get_findings_by_technique(technique_id: str):
    """
    Get all findings associated with a specific technique.

    Args:
        technique_id: MITRE ATT&CK technique ID

    Returns:
        List of findings
    """
    if data_service.is_using_database():
        matching_findings = data_service.get_findings_by_technique(technique_id)
    else:
        matching_findings = []
        for finding in data_service.get_findings():
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
def get_tactics_summary():
    """
    Get summary of tactics across all findings.

    Returns:
        Tactics summary
    """
    if data_service.is_using_database():
        technique_counts = data_service.get_technique_occurrence_counts()
        tactic_counts: dict[str, int] = {}
        for tid, count in technique_counts.items():
            _tid, _name, tactic = resolve_technique(tid)
            tactic_counts[tactic] = tactic_counts.get(tactic, 0) + count
    else:
        findings = data_service.get_findings()
        tactic_counts = {}
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
