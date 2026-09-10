# The built-in backend tools, and the one place they are executed. BACKEND_TOOLS
# is the manifest: a name absent from it is not a backend tool.

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from core.agents.projections import pack_completed_hunts
from core.findings.similarity import similar_findings
from core.memory.recall_contract import RECALL_TOOL

logger = logging.getLogger(__name__)

try:
    from core.llm.tool_schemas import ALL_TOOLS as BACKEND_TOOLS
except ImportError as exc:
    logger.warning("Backend tool schemas unavailable: %s", exc)
    BACKEND_TOOLS: Tuple[Dict[str, Any], ...] = ()

MANIFEST: Dict[str, Dict[str, Any]] = {tool["name"]: tool for tool in BACKEND_TOOLS}

Args = Dict[str, Any]


def _compact(finding: Args) -> Args:
    return {
        "finding_id": finding.get("finding_id"),
        "severity": finding.get("severity"),
        "anomaly_score": float(finding.get("anomaly_score") or 0),
        "data_source": finding.get("data_source"),
        "cluster_id": finding.get("cluster_id"),
        "timestamp": finding.get("timestamp"),
        "status": finding.get("status"),
        "summary": (finding.get("description") or "")[:200],
    }


# Listing and searching differ only in the search filter and the default sort,
# so one page query serves both rather than two that drift apart.
def _page(data: Any, args: Args, *, search: bool) -> Args:
    filters = {key: args.get(key) for key in ("severity", "data_source", "status")}
    if search:
        filters["search_query"] = args.get("query", "")
    limit = args.get("limit", 20)
    offset = args.get("offset", 0)
    total = data.count_findings(**filters)
    findings = data.get_findings(
        limit=limit,
        offset=offset,
        sort_by=args.get("sort_by", "anomaly_score" if search else "timestamp"),
        sort_order=args.get("sort_order", "desc"),
        **filters,
    )
    page = {
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": (offset + limit) < total,
        "findings": [_compact(f) for f in findings],
    }
    return {"query": filters["search_query"], **page} if search else page


def _findings_stats(data: Any, args: Args) -> Args:
    tally: Dict[str, Dict[str, int]] = {
        "by_severity": {},
        "by_data_source": {},
        "by_status": {},
    }
    findings = data.get_findings(limit=10000)
    for finding in findings:
        for key, field in (
            ("by_severity", "severity"),
            ("by_data_source", "data_source"),
            ("by_status", "status"),
        ):
            value = finding.get(field) or "unknown"
            tally[key][value] = tally[key].get(value, 0) + 1
    return {"total_findings": len(findings), **tally}


def _list_cases(data: Any, args: Args) -> Any:
    limit = args.get("limit", 50)
    cases = data.get_cases(limit=limit * 2)
    for field in ("status", "severity"):
        wanted = args.get(field)
        if wanted:
            cases = [case for case in cases if case.get(field) == wanted]
    return cases[:limit]


def _create_case(data: Any, args: Args) -> Any:
    return data.create_case(
        title=args["title"],
        finding_ids=args.get("finding_ids", []),
        priority=args.get("severity", "medium"),
        description=args.get("description", ""),
    )


def _update_case(data: Any, args: Args) -> Args:
    case_id = args.pop("case_id")
    return {"success": data.update_case(case_id, **args), "case_id": case_id}


def _add_resolution_step(data: Any, args: Args) -> Args:
    case = data.get_case(args["case_id"])
    if not case:
        return {"error": f"Case {args['case_id']} not found"}
    steps = case.get("resolution_steps", [])
    steps.append(
        {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "description": args["description"],
            "action_taken": args["action_taken"],
            "result": args.get("result"),
        }
    )
    data.update_case(args["case_id"], resolution_steps=steps)
    return {"success": True, "case_id": args["case_id"], "total_steps": len(steps)}


def _attack_layer(data: Any, args: Args) -> Args:
    return {
        "success": True,
        "layer": {
            "name": "DeepTempo Findings",
            "version": "4.5",
            "domain": "enterprise-attack",
            "description": "ATT&CK techniques from findings",
            "techniques": [],
        },
    }


def _technique_rollup(data: Any, args: Args) -> Args:
    floor = args.get("min_confidence", 0.0)
    counts: Dict[str, int] = {}
    severities: Dict[str, Dict[str, int]] = {}
    for finding in data.get_findings(limit=1000):
        for technique in finding.get("predicted_techniques", []) or []:
            tid = technique.get("technique_id")
            if not tid or technique.get("confidence", 0) < floor:
                continue
            counts[tid] = counts.get(tid, 0) + 1
            bucket = severities.setdefault(
                tid, {"critical": 0, "high": 0, "medium": 0, "low": 0}
            )
            severity = finding.get("severity") or "medium"
            bucket[severity] = bucket.get(severity, 0) + 1
    techniques = [
        {"technique_id": tid, "count": count, "severities": severities[tid]}
        for tid, count in counts.items()
    ]
    techniques.sort(key=lambda entry: entry["count"], reverse=True)
    return {
        "success": True,
        "total_techniques": len(techniques),
        "techniques": techniques,
    }


_DATA_TOOLS: Dict[str, Callable[[Any, Args], Any]] = {
    "list_findings": lambda data, args: _page(data, args, search=False),
    "search_findings": lambda data, args: _page(data, args, search=True),
    "get_findings_stats": _findings_stats,
    "get_finding": lambda data, args: data.get_finding(**args),
    "nearest_neighbors": lambda data, args: similar_findings(data, **args),
    "list_cases": _list_cases,
    "get_case": lambda data, args: data.get_case(**args),
    "create_case": _create_case,
    "add_finding_to_case": lambda data, args: data.add_finding_to_case(
        case_id=args["case_id"], finding_id=args["finding_id"]
    ),
    "update_case": _update_case,
    "add_resolution_step": _add_resolution_step,
    "get_attack_layer": _attack_layer,
    "get_technique_rollup": _technique_rollup,
}


async def _list_completed_hunts(args: Args) -> Any:
    allowed = {key: args[key] for key in ("start", "end", "limit") if key in args}
    return await pack_completed_hunts(**allowed)


_SECURITY_TOOLS = frozenset(
    {
        "analyze_coverage",
        "search_detections",
        "identify_gaps",
        "get_coverage_stats",
        "get_detection_count",
        "lint_detections",
        "reconstruct_run",
    }
)


def _decided(action: Any, verb: str) -> Args:
    if action:
        return asdict(action)
    return {"error": f"Action not found or cannot be {verb}"}


# The local indicator database, fed by the threat-feed poller. A miss is returned
# as a row: an indicator no feed knows is a finding, not an empty answer.
def _indicator_lookup(args: Args) -> Any:
    from core.threat_intel.threat_feed_service import lookup_indicators

    values = args.get("values") or ([args["value"]] if args.get("value") else [])
    if not values:
        raise TypeError("lookup_indicators is missing a required argument: values")

    indicator_type = args.get("indicator_type", "ip")
    hits = lookup_indicators(indicator_type, [str(v) for v in values])
    return [
        {
            "indicator_type": indicator_type,
            "indicator_value": value,
            "known": value in hits,
            **(hits.get(value) or {}),
        }
        for value in values
    ]


_INTEL_TOOLS: Dict[str, Callable[[Args], Any]] = {
    "lookup_indicators": _indicator_lookup,
}


# Episodic memory (#732). One mapping and not a list of rows, so
# tools_router._rows does not slice the Sightings, Verdicts and Gaps into rows of
# their own and lose which list each came from. The import is deferred as the
# other families' are: this module is imported to answer "is this a backend
# tool", which must not drag a database session factory in behind it.
def _recall(args: Args) -> Any:
    from core.memory.recall import recall_entity

    return recall_entity(args)


_MEMORY_TOOLS: Dict[str, Callable[[Args], Any]] = {
    RECALL_TOOL: _recall,
}

_APPROVAL_TOOLS: Dict[str, Callable[[Any, Args], Any]] = {
    "list_pending_approvals": lambda service, args: [
        asdict(action)
        for action in service.list_pending_approvals()[: args.get("limit", 50)]
    ],
    "get_approval_action": lambda service, args: _decided(
        service.get_action(args["action_id"]), "read"
    ),
    "approve_action": lambda service, args: _decided(
        service.approve_action(**args), "approved"
    ),
    "reject_action": lambda service, args: _decided(
        service.reject_action(**args), "rejected"
    ),
    "get_approval_stats": lambda service, args: service.get_stats(),
}


# A skill's tool name is user-authored, so a dispatch failure falls through to
# the table below in case the name merely looks like one.
def _skill_result(
    name: str, args: Args, index: Optional[Args]
) -> Optional[Tuple[Any, bool]]:
    try:
        from core.skills import skill_tools_bridge as skills

        if skills.is_skill_tool_name(name):
            return (
                skills.execute_skill_tool(name, args, skills_by_tool_name=index),
                True,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Skill tool dispatch failed for %s: %s", name, exc)
    return None


# Returns (result, handled). handled is False only when the name is no backend
# tool at all, which is the caller's cue to try MCP.
async def execute_backend_tool(
    tool_name: str,
    tool_input: Optional[Args],
    *,
    skill_index: Optional[Args] = None,
) -> Tuple[Any, bool]:
    args = dict(tool_input or {})

    skill = _skill_result(tool_name, args, skill_index)
    if skill is not None:
        return skill

    if tool_name in _DATA_TOOLS:
        from core.storage.database_data_service import DatabaseDataService

        return _DATA_TOOLS[tool_name](DatabaseDataService(), args), True

    if tool_name == "list_completed_hunts":
        return await _list_completed_hunts(args), True

    if tool_name in _SECURITY_TOOLS:
        from core.detections.tools import get_security_detection_tools

        handler = getattr(get_security_detection_tools(), tool_name, None)
        if handler is None:
            return {"error": f"Unknown tool: {tool_name}"}, True
        return await handler(**args), True

    if tool_name in _INTEL_TOOLS:
        return _INTEL_TOOLS[tool_name](args), True

    if tool_name in _MEMORY_TOOLS:
        return _MEMORY_TOOLS[tool_name](args), True

    if tool_name in _APPROVAL_TOOLS:
        from core.response.approval_service import ApprovalService

        return _APPROVAL_TOOLS[tool_name](ApprovalService(), args), True

    return None, False
