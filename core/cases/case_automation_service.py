"""VStrike attack-path clustering into cases."""

import logging
from typing import Dict, List

from core.cases.case_sla_service import open_case

logger = logging.getLogger(__name__)


_SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def _max_severity(findings: List[Dict]) -> str:
    best = "low"
    for f in findings:
        sev = (f.get("severity") or "low").lower()
        if _SEVERITY_ORDER.get(sev, 0) > _SEVERITY_ORDER.get(best, 0):
            best = sev
    return best


def cluster_findings_by_attack_path(finding_ids: List[str]) -> List[str]:
    """Group VStrike-enriched findings into cases by (segment, attack_path[0]).

    For each cluster with at least one finding, create a case titled
    "VStrike: {segment} via {initial_asset}". Returns the list of newly
    created case_ids.

    Findings without a `vstrike` sub-dict in `entity_context` are skipped —
    this function is VStrike-specific by design.
    """
    from core.storage.database_data_service import DatabaseDataService

    data_service = DatabaseDataService()

    clusters: Dict[tuple, List[Dict]] = {}
    for fid in finding_ids:
        finding = data_service.get_finding(fid)
        if not finding:
            continue
        ctx = finding.get("entity_context") or {}
        vstrike = ctx.get("vstrike") if isinstance(ctx, dict) else None
        if not vstrike:
            continue
        segment = vstrike.get("segment") or "unknown-segment"
        attack_path = vstrike.get("attack_path") or []
        initial_asset = (
            attack_path[0]
            if attack_path
            else (vstrike.get("asset_id") or "unknown-asset")
        )
        key = (segment, initial_asset)
        clusters.setdefault(key, []).append(finding)

    created_case_ids: List[str] = []
    for (segment, initial_asset), findings in clusters.items():
        ids = [f.get("finding_id") for f in findings if f.get("finding_id")]
        if not ids:
            continue

        severity = _max_severity(findings)
        priority = severity  # treat severity as priority for VStrike cases
        mission_systems = sorted(
            {
                (f.get("entity_context") or {}).get("vstrike", {}).get("mission_system")
                for f in findings
                if (f.get("entity_context") or {})
                .get("vstrike", {})
                .get("mission_system")
            }
        )
        mission_str = (
            f" Mission systems impacted: {', '.join(mission_systems)}."
            if mission_systems
            else ""
        )
        description = (
            f"{len(findings)} enriched finding(s) from the VStrike fusion layer "
            f"across segment '{segment}'. Attack path origin: {initial_asset}."
            f"{mission_str}"
        )
        title = f"VStrike: {segment} via {initial_asset}"

        case = open_case(
            data_service,
            title=title,
            finding_ids=ids,
            priority=priority,
            description=description,
            status="open",
        )
        if case and case.get("case_id"):
            created_case_ids.append(case["case_id"])
            logger.info(
                "Created VStrike auto-cluster case %s (%s findings)",
                case["case_id"],
                len(ids),
            )

    return created_case_ids
