"""Shared tool loading + filtering for the agent loops.

Both ``ClaudeService`` (Anthropic path) and ``OpenAIAgentService`` (OpenAI-
format path) load the same backend tool schemas and DB-backed skill tools and
apply the same per-agent ``recommended_tools`` filter. Centralizing that logic
here keeps the two loops from drifting (a bug fixed in one used to need fixing
in the other).

All functions are stateless and best-effort: they degrade to empty results
rather than raising, so a missing backend-schema import or an unavailable
skills DB never breaks a chat request.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def load_backend_tools() -> List[Dict[str, Any]]:
    """Return the static backend tool schemas (empty list if unavailable)."""
    try:
        from backend.schemas.tool_schemas import ALL_TOOLS

        return list(ALL_TOOLS)
    except ImportError:
        logger.debug("Backend tool schemas unavailable")
        return []


def load_skill_tools() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return ``(skill_tools, skill_index)`` for active DB-backed skills.

    Best-effort — returns ``([], {})`` when the skill bridge or its DB is
    unavailable.
    """
    try:
        from services.skill_tools_bridge import list_active_skill_tools

        tools, index = list_active_skill_tools()
        return list(tools), index
    except Exception as exc:  # noqa: BLE001
        logger.debug("Skill tools unavailable: %s", exc)
        return [], {}


def filter_tools_by_recommended(
    tools: List[Dict[str, Any]], recommended: Optional[List[str]]
) -> List[Dict[str, Any]]:
    """Keep only tools whose name is in ``recommended``; no-op when falsy.

    MCP/server tools arrive prefixed as ``<server>_<tool>`` while per-agent
    recommended lists (``soc_agents.py``) use bare names, so a tool matches if
    either its full name or its post-first-underscore suffix is recommended.
    """
    if not recommended:
        return tools
    wanted = set(recommended)
    out: List[Dict[str, Any]] = []
    for t in tools:
        name = t.get("name", "")
        if name in wanted:
            out.append(t)
        elif "_" in name and name.split("_", 1)[1] in wanted:
            out.append(t)
    return out


# --- Tool safety tiers ----------------------------------------------------
#
# Single source of truth for the action-safety classification an agent's tool
# calls are gated against. Owned here (not in the daemon) so every agent loop —
# the daemon's autonomous investigator, the interactive OpenAI agent, and the
# workflow engine — enforces the *same* policy. ``requires_approval`` tools
# never execute without a human decision; ``forbidden`` tools never execute
# under autonomous control at all.

TOOL_TIERS: Dict[str, List[str]] = {
    "safe": [
        "list_findings",
        "get_finding",
        "search_findings",
        "nearest_neighbors",
        "get_findings_stats",
        "semantic_search_findings",
        "technique_rollup",
        "list_cases",
        "get_case",
        "get_case_comments",
        "get_case_iocs",
        "get_case_tasks",
        "search_detections",
        "get_coverage_stats",
        "get_detection_count",
        "analyze_coverage",
        "identify_gaps",
        "create_attack_layer",
        "get_attack_layer",
    ],
    "managed": [
        "create_case",
        "update_case",
        "add_finding_to_case",
        "bulk_add_findings_to_case",
        "remove_finding_from_case",
        "add_case_activity",
        "add_case_timeline_entry",
        "add_case_mitre_techniques",
        "add_resolution_step",
        "add_case_comment",
        "add_case_evidence",
        "add_case_ioc",
        "bulk_add_iocs",
        "add_case_task",
        "update_case_task",
        "link_related_cases",
        "escalate_case",
        "create_approval_action",
    ],
    "requires_approval": [
        "isolate_host",
        "block_ip",
        "disable_user",
        "quarantine_file",
        "close_case",
    ],
    "forbidden": [
        "delete_case",
        "delete_finding",
        "approve_action",
        "reject_action",
    ],
}

_TOOL_TIER_LOOKUP: Dict[str, str] = {}
for _tier, _tier_tools in TOOL_TIERS.items():
    for _tier_tool in _tier_tools:
        _TOOL_TIER_LOOKUP[_tier_tool] = _tier


# Destructive/containment action verbs. A tool with one of these as a whole
# token requires approval even when its exact name isn't in TOOL_TIERS: vendor
# MCP tools are double-prefixed (crowdstrike_cs_isolate_host) so neither the
# exact nor the suffix lookup below reaches the generic tier names
# (isolate_host) — without this floor every vendor containment tool resolves to
# "unknown" and executes with no approval. Whole-token match (not substring) so
# "skill_*" doesn't trip "kill" and "get_container_*" doesn't trip "contain".
_ACTION_VERB_TOKENS = frozenset(
    {
        # endpoint/network containment (+ reversals — still state-changing)
        "isolate",
        "unisolate",
        "block",
        "unblock",
        "quarantine",
        "contain",
        # account/session state
        "disable",
        "deactivate",
        "suspend",
        "revoke",
        "reset",
        "deprovision",
        # process/host lifecycle
        "terminate",
        "kill",
        "shutdown",
        "reboot",
        "restart",
        # data destruction
        "purge",
        "wipe",
        "delete",
    }
)


# Split only at a lowercase->uppercase boundary so camelCase vendor names
# tokenize (``suspendUser`` -> ``suspend``/``user``) without shredding an
# all-caps run: ``ISOLATE_HOST`` and acronyms like ``blockIP`` stay whole.
# See _has_action_verb.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z])(?=[A-Z])")


def _has_action_verb(tool_name: str) -> bool:
    """True if any token is a destructive action verb.

    Splits on ``_``/``-`` and camelCase boundaries before whole-token matching,
    so ``suspendUser`` tokenizes to ``suspend``/``user`` instead of the single
    unmatched token ``suspenduser``.
    """
    normalized = _CAMEL_BOUNDARY.sub("_", tool_name).replace("-", "_").lower()
    return any(tok in _ACTION_VERB_TOKENS for tok in normalized.split("_"))


# First-party tool namespaces exempt from the destructive-verb floor, so a
# verb in their name doesn't stall investigations on human approval:
#   mempalace_  — internal memory housekeeping, not a security action
#                 (mempalace_delete_drawer is routine memory-entry cleanup;
#                  a future mempalace_purge_*/reset_* would trip too).
#   skill_      — a DB-backed Skill only renders a prompt template and returns
#                 text (no state change); its slug comes from a user-authored
#                 name, so "Isolate Host Playbook" -> skill_isolate_host_playbook
#                 would otherwise gate. Any real action stays separately gated.
_VERB_FLOOR_EXEMPT_PREFIXES = ("mempalace_", "skill_")


def get_tool_tier(tool_name: str) -> str:
    """Return the safety tier for ``tool_name``.

    Resolution order: exact name, then the post-first-underscore suffix (drops
    the MCP ``{server}_`` prefix), then a destructive-verb floor that lifts
    otherwise-``unknown`` action tools to ``requires_approval`` (see
    ``_ACTION_VERB_TOKENS``) — skipping the floor for first-party namespaces in
    ``_VERB_FLOOR_EXEMPT_PREFIXES``. Returns ``"unknown"`` for tools in no tier
    — callers treat unknown as executable-but-uncategorized (historical daemon
    behavior).
    """
    if tool_name in _TOOL_TIER_LOOKUP:
        return _TOOL_TIER_LOOKUP[tool_name]
    short = tool_name.split("_", 1)[-1] if "_" in tool_name else tool_name
    if short in _TOOL_TIER_LOOKUP:
        return _TOOL_TIER_LOOKUP[short]
    if tool_name.startswith(_VERB_FLOOR_EXEMPT_PREFIXES):
        return "unknown"
    if _has_action_verb(tool_name):
        return "requires_approval"
    return "unknown"


# --- Backend tool dispatch ------------------------------------------------
#
# Single source of truth for executing built-in (non-MCP) backend tools.
# Currently used by OpenAIAgentService. (ClaudeService and the daemon loop
# still use their own dispatch table for now, but will be consolidated here
# in the future).

_FINDINGS_CASE_TOOLS = frozenset(
    {
        "list_findings",
        "get_finding",
        "nearest_neighbors",
        "search_findings",
        "get_findings_stats",
        "list_cases",
        "get_case",
        "create_case",
        "add_finding_to_case",
        "update_case",
        "add_resolution_step",
    }
)
_SECURITY_TOOLS = frozenset(
    {
        "analyze_coverage",
        "search_detections",
        "identify_gaps",
        "get_coverage_stats",
        "get_detection_count",
    }
)
_ATTACK_TOOLS = frozenset({"get_attack_layer", "get_technique_rollup"})
_APPROVAL_TOOLS = frozenset(
    {
        "list_pending_approvals",
        "get_approval_action",
        "approve_action",
        "reject_action",
        "get_approval_stats",
    }
)


async def execute_backend_tool(
    tool_name: str,
    arguments: Optional[Dict[str, Any]],
    *,
    skill_index: Optional[Dict[str, Any]] = None,
) -> Tuple[Any, bool]:
    """Execute a built-in backend tool, returning ``(result, handled)``.

    ``handled`` is ``True`` when ``tool_name`` matched a backend handler (even
    if that handler returned an ``{"error": ...}`` payload). When ``False`` the
    caller should fall back to the MCP layer.

    Sync data/case/approval handlers run inline; async security-detection
    handlers are awaited. All optional imports are best-effort so a missing
    dependency degrades to an error payload rather than raising into the loop.
    """
    arguments = arguments or {}

    # DB-backed Skills (Issue #82) get a dedicated dispatch so we don't bury
    # user-created tools in the ladder below. On dispatch failure we fall
    # through so a name that merely *looks* like a skill still tries the rest
    # of the chain.
    try:
        from services.skill_tools_bridge import execute_skill_tool, is_skill_tool_name

        if is_skill_tool_name(tool_name):
            return (
                execute_skill_tool(
                    tool_name, arguments, skills_by_tool_name=skill_index
                ),
                True,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Skill tool dispatch failed for %s: %s", tool_name, exc)

    if tool_name in _FINDINGS_CASE_TOOLS:
        from services.database_data_service import DatabaseDataService

        return _execute_findings_case_tool(DatabaseDataService(), tool_name, arguments)

    if tool_name in _SECURITY_TOOLS:
        from tools.security_detections import get_security_detection_tools

        handler = getattr(get_security_detection_tools(), tool_name, None)
        if handler is None:
            return {"error": f"Unknown tool: {tool_name}"}, True
        return await handler(**arguments), True

    if tool_name in _ATTACK_TOOLS:
        from services.database_data_service import DatabaseDataService

        return _execute_attack_tool(DatabaseDataService(), tool_name, arguments), True

    if tool_name in _APPROVAL_TOOLS:
        return _execute_approval_tool(tool_name, arguments), True

    return None, False


def _compact_finding(f: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "finding_id": f.get("finding_id"),
        "severity": f.get("severity"),
        "anomaly_score": float(f.get("anomaly_score") or 0),
        "data_source": f.get("data_source"),
        "cluster_id": f.get("cluster_id"),
        "timestamp": f.get("timestamp"),
        "status": f.get("status"),
        "summary": (f.get("description") or "")[:200],
    }


def _execute_findings_case_tool(
    data_service: Any, tool_name: str, args: Dict[str, Any]
) -> Tuple[Any, bool]:
    if tool_name == "list_findings":
        limit = args.get("limit", 20)
        offset = args.get("offset", 0)
        severity = args.get("severity")
        data_source = args.get("data_source")
        status = args.get("status")
        total = data_service.count_findings(
            severity=severity, data_source=data_source, status=status
        )
        findings = data_service.get_findings(
            limit=limit,
            offset=offset,
            severity=severity,
            data_source=data_source,
            status=status,
            sort_by=args.get("sort_by", "timestamp"),
            sort_order=args.get("sort_order", "desc"),
        )
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": (offset + limit) < total,
            "findings": [_compact_finding(f) for f in findings],
        }, True

    if tool_name == "search_findings":
        query = args.get("query", "")
        limit = args.get("limit", 20)
        offset = args.get("offset", 0)
        severity = args.get("severity")
        data_source = args.get("data_source")
        status = args.get("status")
        total = data_service.count_findings(
            severity=severity,
            data_source=data_source,
            status=status,
            search_query=query,
        )
        findings = data_service.get_findings(
            limit=limit,
            offset=offset,
            severity=severity,
            data_source=data_source,
            status=status,
            search_query=query,
            sort_by=args.get("sort_by", "anomaly_score"),
            sort_order=args.get("sort_order", "desc"),
        )
        return {
            "query": query,
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": (offset + limit) < total,
            "findings": [_compact_finding(f) for f in findings],
        }, True

    if tool_name == "get_findings_stats":
        findings = data_service.get_findings(limit=10000)
        severity_counts: Dict[str, int] = {}
        data_source_counts: Dict[str, int] = {}
        status_counts: Dict[str, int] = {}
        for f in findings:
            sev = f.get("severity") or "unknown"
            severity_counts[sev] = severity_counts.get(sev, 0) + 1
            ds = f.get("data_source") or "unknown"
            data_source_counts[ds] = data_source_counts.get(ds, 0) + 1
            st = f.get("status") or "unknown"
            status_counts[st] = status_counts.get(st, 0) + 1
        return {
            "total_findings": len(findings),
            "by_severity": severity_counts,
            "by_data_source": data_source_counts,
            "by_status": status_counts,
        }, True

    if tool_name == "get_finding":
        return data_service.get_finding(**args), True

    if tool_name == "nearest_neighbors":
        return data_service.get_nearest_neighbors(**args), True

    if tool_name == "list_cases":
        limit = args.get("limit", 50)
        status = args.get("status")
        severity = args.get("severity")
        cases = data_service.get_cases(limit=limit * 2)
        if status:
            cases = [c for c in cases if c.get("status") == status]
        if severity:
            cases = [c for c in cases if c.get("severity") == severity]
        return cases[:limit], True

    if tool_name == "get_case":
        return data_service.get_case(**args), True

    if tool_name == "create_case":
        return (
            data_service.create_case(
                title=args["title"],
                finding_ids=args.get("finding_ids", []),
                priority=args.get("severity", "medium"),
                description=args.get("description", ""),
            ),
            True,
        )

    if tool_name == "add_finding_to_case":
        return (
            data_service.add_finding_to_case(
                case_id=args["case_id"], finding_id=args["finding_id"]
            ),
            True,
        )

    if tool_name == "update_case":
        uc_args = dict(args)
        case_id = uc_args.pop("case_id")
        success = data_service.update_case(case_id, **uc_args)
        return {"success": success, "case_id": case_id}, True

    if tool_name == "add_resolution_step":
        from datetime import datetime as _dt

        case = data_service.get_case(args["case_id"])
        if not case:
            return {"error": f"Case {args['case_id']} not found"}, True
        steps = case.get("resolution_steps", [])
        steps.append(
            {
                "timestamp": _dt.utcnow().isoformat() + "Z",
                "description": args["description"],
                "action_taken": args["action_taken"],
                "result": args.get("result"),
            }
        )
        data_service.update_case(args["case_id"], resolution_steps=steps)
        return {
            "success": True,
            "case_id": args["case_id"],
            "total_steps": len(steps),
        }, True

    return {"error": f"Unknown tool: {tool_name}"}, True


def _execute_attack_tool(
    data_service: Any, tool_name: str, args: Dict[str, Any]
) -> Any:
    if tool_name == "get_attack_layer":
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

    # get_technique_rollup
    min_conf = args.get("min_confidence", 0.0) if args else 0.0
    findings = data_service.get_findings(limit=1000)
    counts: Dict[str, int] = {}
    severities: Dict[str, Dict[str, int]] = {}
    for f in findings:
        for tech in f.get("predicted_techniques", []) or []:
            tid = tech.get("technique_id")
            conf = tech.get("confidence", 0)
            if conf < min_conf or not tid:
                continue
            counts[tid] = counts.get(tid, 0) + 1
            if tid not in severities:
                severities[tid] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
            sev = f.get("severity") or "medium"
            severities[tid][sev] = severities[tid].get(sev, 0) + 1
    techniques = [
        {"technique_id": t, "count": c, "severities": severities[t]}
        for t, c in counts.items()
    ]
    techniques.sort(key=lambda x: x["count"], reverse=True)
    return {
        "success": True,
        "total_techniques": len(techniques),
        "techniques": techniques,
    }


def _execute_approval_tool(tool_name: str, args: Dict[str, Any]) -> Any:
    from dataclasses import asdict

    from services.approval_service import ApprovalService

    approval_service = ApprovalService()

    if tool_name == "list_pending_approvals":
        actions = approval_service.list_pending_approvals()
        return [asdict(a) for a in actions[: args.get("limit", 50)]]
    if tool_name == "get_approval_action":
        action = approval_service.get_action(args["action_id"])
        return asdict(action) if action else {"error": "Action not found"}
    if tool_name == "approve_action":
        action = approval_service.approve_action(**args)
        return (
            asdict(action)
            if action
            else {"error": "Action not found or cannot be approved"}
        )
    if tool_name == "reject_action":
        action = approval_service.reject_action(**args)
        return (
            asdict(action)
            if action
            else {"error": "Action not found or cannot be rejected"}
        )
    # get_approval_stats
    return approval_service.get_stats()
