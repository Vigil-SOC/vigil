"""Finding counts per ATT&CK technique.

The ATT&CK page and the agent tool both read this. It lives outside the FastAPI
router so a tool can call it without importing the API.
"""

from datetime import datetime
from typing import Optional

from core.storage.database_data_service import DatabaseDataService
from core.threat_intel.mitre_lookup import (
    get_time_range,
    iter_techniques,
    resolve_technique,
)


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


def occurrence_rollup(
    min_confidence: float = 0.0,
    time_range: str = "all",
    service=None,
):
    data_service = service if service is not None else DatabaseDataService()
    if data_service.is_using_database():
        start_time = end_time = None
        if time_range != "all":
            start_time, end_time = get_time_range(time_range)
        # The dashboard's counts describe the queue, which analyst-excluded IPs
        # are not in (core.findings.exclusions); a coverage run keeps them.
        severity_rows = data_service.get_technique_severity_counts(
            min_confidence=min_confidence,
            start_time=start_time,
            end_time=end_time,
            exclusions="hide",
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
        findings = data_service.get_findings(exclusions="hide")

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
