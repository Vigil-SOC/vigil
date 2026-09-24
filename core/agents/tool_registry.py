# The built-in backend tools, and the one place they are executed. BACKEND_TOOLS
# is the manifest: a name absent from it is not a backend tool.

from __future__ import annotations

import inspect
import logging
from dataclasses import asdict
from typing import Any, Callable, Dict, Optional, Tuple

from core.agents.projections import pack_completed_hunts, read_replay
from core.memory.recall_contract import RECALL_TOOL
from core.skills.skill_library import READ_SKILL_TOOL, read_skill

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


def _data():
    from core.storage.database_data_service import DatabaseDataService

    return DatabaseDataService()


def _approvals():
    from core.response.approval_service import ApprovalService

    return ApprovalService()


def list_findings(
    *,
    severity: Optional[str] = None,
    data_source: Optional[str] = None,
    status: Optional[str] = None,
    cluster_id: Optional[str] = None,
    min_anomaly_score: Optional[float] = None,
    sort_by: str = "timestamp",
    sort_order: str = "desc",
    offset: int = 0,
    limit: int = 20,
) -> Args:
    filters = {
        "severity": severity,
        "data_source": data_source,
        "status": status,
        "cluster_id": cluster_id,
        "min_anomaly_score": min_anomaly_score,
    }
    data = _data()
    total = data.count_findings(**filters)
    findings = data.get_findings(
        limit=limit,
        offset=offset,
        sort_by=sort_by,
        sort_order=sort_order,
        **filters,
    )
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": (offset + limit) < total,
        "findings": [_compact(f) for f in findings],
    }


def search_findings(**args: Any) -> Args:
    return _page(_data(), args, search=True)


def get_findings_stats(**_args: Any) -> Args:
    return _findings_stats(_data(), {})


def get_finding(*, finding_id: str) -> Any:
    return _data().get_finding(finding_id)


def list_cases(
    *,
    status: Optional[str] = None,
    severity: Optional[str] = None,
    priority: Optional[str] = None,
    limit: int = 50,
) -> Any:
    # get_cases takes only a cap, so the filter runs after the full read.
    # A cap applied first (limit*2) drops matches that sit past it.
    cases = _data().get_cases()
    if status:
        cases = [case for case in cases if case.get("status") == status]
    if severity:
        cases = [
            case
            for case in cases
            if case.get("severity") == severity or case.get("priority") == severity
        ]
    if priority:
        cases = [case for case in cases if case.get("priority") == priority]
    return cases[:limit]


def get_case(*, case_id: str) -> Any:
    return _data().get_case(case_id)


def create_case(
    *,
    title: str,
    description: str = "",
    severity: Optional[str] = None,
    priority: Optional[str] = None,
    finding_ids: Optional[list] = None,
    status: str = "new",
    assignee: Optional[str] = None,
    tags: Optional[list] = None,
) -> Args:
    data = _data()
    ids = finding_ids or []
    case = data.create_case(
        title=title,
        finding_ids=ids,
        priority=priority or severity or "medium",
        description=description,
        status=status,
    )
    if not case:
        return {"error": "Failed to create case"}
    # assignee and tags are edits, applied after the case exists.
    edits: Args = {}
    if assignee is not None:
        edits["assignee"] = assignee
    if tags:
        edits["tags"] = tags
    if edits:
        data.update_case(case["case_id"], **edits)
        case = data.get_case(case["case_id"]) or case
    return {
        "success": True,
        "case_id": case.get("case_id"),
        "title": case.get("title"),
        "status": case.get("status"),
        "finding_count": len(ids),
    }


def update_case(
    *,
    case_id: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    assignee: Optional[str] = None,
    add_note: Optional[str] = None,
) -> Args:
    from core.cases.agent_closure import record_agent_close, record_reopen
    from core.time import utcnow

    data = _data()
    case = data.get_case(case_id)
    if not case:
        return {"error": f"Case {case_id} not found"}

    updates: Args = {}
    if title:
        updates["title"] = title
    if description:
        updates["description"] = description
    if status:
        updates["status"] = status
    if priority:
        updates["priority"] = priority
    if assignee:
        updates["assignee"] = assignee
    if add_note:
        notes = list(case.get("notes") or [])
        notes.append({"timestamp": utcnow().isoformat() + "Z", "content": add_note})
        updates["notes"] = notes

    was_closed = (case.get("status") or "").strip() == "closed"
    if not data.update_case(case_id, **updates):
        return {"error": "Failed to update case"}
    if updates.get("status") == "closed" and not was_closed:
        record_agent_close(case_id)
    elif was_closed and updates.get("status") not in (None, "closed"):
        record_reopen(case_id)
    return {"success": True, "case_id": case_id}


# Missing case or empty records must still be a result: an error here parks the
# lead before it can start. Empty sections are the answer, not refused.
def _case_records(args: Args) -> Args:
    from core.cases.case_records_service import list_escalations, list_tasks
    from core.storage.schemas.case_entities import CaseEscalationSchema, CaseTaskSchema
    from core.storage.unit_of_work import unit_of_work

    case_id = str(args.get("case_id") or "")
    try:
        tasks = CaseTaskSchema.dump_many(list_tasks(case_id))
    except Exception:
        logger.exception("Listing tasks for case %s failed; reporting none", case_id)
        tasks = []
    try:
        with unit_of_work() as session:
            escalations = CaseEscalationSchema.dump_many(
                list_escalations(session, case_id)
            )
    except Exception:
        logger.exception(
            "Listing escalations for case %s failed; reporting none", case_id
        )
        escalations = []
    return {"tasks": tasks, "escalations": escalations}


def add_finding_to_case(*, case_id: str, finding_id: str) -> Args:
    from core.cases import case_journal_service

    linked = case_journal_service.link_finding(_data(), case_id, finding_id)
    if linked is None:
        return {"error": f"Failed to add {finding_id} to {case_id}"}
    return {
        "success": True,
        "message": (
            f"Added {finding_id} to {case_id}"
            if linked
            else f"{finding_id} was already on {case_id}"
        ),
    }


def add_resolution_step(
    *,
    case_id: str,
    description: str,
    action_taken: str,
    result: Optional[str] = None,
) -> Args:
    from core.cases import case_journal_service

    step = case_journal_service.append_resolution_step(
        _data(),
        case_id,
        description=description,
        action_taken=action_taken,
        result=result,
    )
    if step is None:
        return {"error": f"Failed to add resolution step to {case_id}"}
    return {
        "success": True,
        "message": f"Added resolution step to {case_id}",
        "step": step,
    }


def get_technique_rollup(
    *, min_confidence: float = 0.0, time_range: str = "all"
) -> Args:
    from core.threat_intel.occurrence_rollup import occurrence_rollup

    return occurrence_rollup(
        min_confidence=min_confidence,
        time_range=time_range,
        service=_data(),
    )


async def list_completed_hunts(*, start: str, end: str, limit: int = 200) -> Any:
    return await pack_completed_hunts(start=start, end=end, limit=limit)


# None from the read is "nothing to replay"; a model needs a body, not null.
async def replay_hunt(*, run_id: str, decision_id: Optional[str] = None) -> Any:
    if not run_id:
        raise TypeError("replay_hunt is missing a required argument: run_id")
    report = await read_replay(str(run_id), decision_id or None)
    return (
        report
        if report is not None
        else {"error": f"Nothing to replay for run {run_id}"}
    )


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


# Recent feed rows through the coverage check (#905). Proposes; never hunts.
def _propose_feed_hunts(args: Args) -> Any:
    from core.threat_intel.threat_feed_service import (
        RECENT_INDICATOR_LIMIT,
        propose_hunts_from_recent_indicators,
    )

    try:
        limit = int(args.get("limit", RECENT_INDICATOR_LIMIT))
    except (TypeError, ValueError):
        return {"error": f"limit must be an integer, got {args.get('limit')!r}"}
    return propose_hunts_from_recent_indicators(limit=limit)


_INTEL_TOOLS: Dict[str, Callable[[Args], Any]] = {
    "lookup_indicators": _indicator_lookup,
    "propose_feed_hunts": _propose_feed_hunts,
}


# Episodic memory (#732). One mapping and not a list of rows, so
# tools_router._rows does not slice the Sightings, Verdicts and Gaps into rows of
# their own and lose which list each came from. The import is deferred as the
# other families' are: this module is imported to answer "is this a backend
# tool", which must not drag a database session factory in behind it.
def _recall(args: Args) -> Any:
    from core.memory.recall import recall_entity

    return recall_entity(args)


# Report in, one of three answers out (#903). Reads only; never starts a hunt.
def _check_hunt_coverage(args: Args) -> Any:
    from core.memory.hunt_coverage import check_coverage

    try:
        return check_coverage(
            report=args.get("report"),
            entity_keys=args.get("entity_keys") or (),
            techniques=args.get("techniques") or (),
        )
    except ValueError as exc:
        return {"error": str(exc)}


# Learning episodes (#906): a window over Distil markers, and a JSONL export of a
# chosen subset. Mappings, for the same reason as _recall.
def _list_learning_episodes(args: Args) -> Any:
    from core.memory.learning_episodes import list_learning_episodes

    return list_learning_episodes(args)


def _export_learning_episodes(args: Args) -> Any:
    from core.memory.learning_episodes import export_learning_episodes

    return export_learning_episodes(args)


_MEMORY_TOOLS: Dict[str, Callable[[Args], Any]] = {
    RECALL_TOOL: _recall,
    "check_hunt_coverage": _check_hunt_coverage,
    "list_learning_episodes": _list_learning_episodes,
    "export_learning_episodes": _export_learning_episodes,
}


def list_pending_approvals(**args: Any) -> Any:
    limit = args.get("limit", 50)
    return [asdict(action) for action in _approvals().list_pending_approvals()[:limit]]


def get_approval_action(*, action_id: str) -> Args:
    return _decided(_approvals().get_action(action_id), "read")


# The actor is the caller, not an argument. A model that names one is choosing
# what the record will say.
def approve_action(*, action_id: str) -> Args:
    from core.cases.agent_closure import actor

    return _decided(_approvals().approve_action(action_id, actor()), "approved")


def reject_action(*, action_id: str, reason: str) -> Args:
    from core.cases.agent_closure import actor

    return _decided(_approvals().reject_action(action_id, reason, actor()), "rejected")


def get_approval_stats(**_args: Any) -> Any:
    return _approvals().get_stats()


# Names executed by the function of the same name in this module. Looked up
# when called, so the MCP wrappers and this door share that function.
_OWNED = frozenset(
    {
        "list_findings",
        "search_findings",
        "get_findings_stats",
        "get_finding",
        "list_cases",
        "get_case",
        "create_case",
        "add_finding_to_case",
        "update_case",
        "add_resolution_step",
        "get_technique_rollup",
        "list_completed_hunts",
        "replay_hunt",
        "list_pending_approvals",
        "get_approval_action",
        "approve_action",
        "reject_action",
        "get_approval_stats",
    }
)


# Returns (result, handled). handled is False only when the name is no backend
# tool at all, which is the caller's cue to try MCP.
async def execute_backend_tool(
    tool_name: str,
    tool_input: Optional[Args],
) -> Tuple[Any, bool]:
    args = dict(tool_input or {})

    if tool_name == "case_records":
        return _case_records(args), True

    if tool_name in _OWNED:
        result = globals()[tool_name](**args)
        if inspect.isawaitable(result):
            result = await result
        return result, True

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

    # Agent skills (#925): reads from disk only, never a database.
    if tool_name == READ_SKILL_TOOL:
        return read_skill(args.get("name"), args.get("file")), True

    return None, False
