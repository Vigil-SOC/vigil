# The built-in backend tools, and the one place they are executed. BACKEND_TOOLS
# is the manifest: a name absent from it is not a backend tool.

from __future__ import annotations

import inspect
import json
import logging
from contextlib import nullcontext
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Tuple

from core.agents.attribution import current_claims
from core.agents.mcp_tools import MCPFailure
from core.agents.projections import pack_completed_hunts, read_replay
from core.auth.permissions import APPROVE_PERMISSION, permission_refusal
from core.cases.case_sla_service import open_case
from core.cases.case_status import (
    CASE_STATUSES,
    CaseStatus,
    record_status_change,
    status_change,
)
from core.cases.linked_entities import finding_entity_summary
from core.findings.anomaly_score import stored_anomaly_score
from core.llm.tool_schemas import ALL_TOOLS as BACKEND_TOOLS
from core.llm.tool_schemas import (
    CALL_INTEGRATION_TOOL,
    FIND_INTEGRATION_TOOLS,
    INTEGRATION_TOOLS,
)
from core.memory.recall_contract import RECALL_TOOL
from core.skills.skill_library import READ_SKILL_TOOL, read_skill

if TYPE_CHECKING:
    from core.integrations.mcp.registry import MCPRegistry

logger = logging.getLogger(__name__)

# The integration tools are chat's alone (#1959), so they are not in ALL_TOOLS.
# call_integration_tool is unwrapped by tools_router into the call it stands for.
MANIFEST: Dict[str, Dict[str, Any]] = {
    tool["name"]: tool for tool in (*BACKEND_TOOLS, *INTEGRATION_TOOLS)
}

Args = Dict[str, Any]

# Every chat tool names the permission execute_backend_tool checks. A run no
# person started is bound by its run claims and holds no role: the autonomy
# ceiling governs it at invoke. A caller bound to nothing is refused.
TOOL_PERMISSIONS: Dict[str, str] = {
    "analyze_coverage": "detections.read",
    "search_detections": "detections.read",
    "identify_gaps": "detections.read",
    "get_coverage_stats": "detections.read",
    "get_detection_count": "detections.read",
    "lint_detections": "detections.read",
    "check_detection_candidate": "detections.read",
    "reconstruct_run": "detections.read",
    "list_findings": "findings.read",
    "search_findings": "findings.read",
    "get_findings_stats": "findings.read",
    "get_finding": "findings.read",
    "dismiss_finding": "findings.write",
    "list_cases": "cases.read",
    "get_case": "cases.read",
    "case_records": "cases.read",
    "create_case": "cases.write",
    "add_finding_to_case": "cases.write",
    "update_case": "cases.write",
    "add_resolution_step": "cases.write",
    "list_completed_hunts": "automation.read",
    "replay_hunt": "automation.read",
    "get_technique_rollup": "findings.read",
    "list_pending_approvals": "ai_decisions.read",
    "get_approval_action": "ai_decisions.read",
    "approve_action": APPROVE_PERMISSION,
    "reject_action": APPROVE_PERMISSION,
    "get_approval_stats": "ai_decisions.read",
    "lookup_indicators": "findings.read",
    "propose_feed_hunts": "findings.read",
    RECALL_TOOL: "automation.read",
    "check_hunt_coverage": "automation.read",
    "list_learning_episodes": "automation.read",
    "export_learning_episodes": "automation.read",
    READ_SKILL_TOOL: "automation.read",
    FIND_INTEGRATION_TOOLS: "integrations.read",
    CALL_INTEGRATION_TOOL: "integrations.write",
}


def required_permission(tool: str) -> Optional[str]:
    """The permission one of Vigil's own tools checks, or None for any other name.

    A built-in names it here; a tool on Vigil's MCP server names it with
    ``@requires``.
    """
    if tool in TOOL_PERMISSIONS:
        return TOOL_PERMISSIONS[tool]
    from core.integrations.mcp import in_process

    return in_process.required_permission(tool)


# A page of findings is what a hunt groups alerts by, so each carries the hosts,
# users and indicators it names (#2277), bounded; the raw record stays behind
# get_finding, which a hunt opens only for the alerts worth reading in full.
def _compact(finding: Args, state: Optional[str] = None) -> Args:
    return {
        "finding_id": finding.get("finding_id"),
        "title": finding.get("title"),
        "severity": finding.get("severity"),
        "anomaly_score": stored_anomaly_score(finding),
        "data_source": finding.get("data_source"),
        "cluster_id": finding.get("cluster_id"),
        "timestamp": finding.get("timestamp"),
        "state": state,
        "summary": (finding.get("description") or "")[:200],
        "entities": finding_entity_summary(finding.get("entity_context")),
    }


# Addresses each IP facet of a search lists; the rest are counted, not named.
TOP_IPS = 10


def _compact_page(data: Any, findings: list) -> list:
    from core.findings.alert_outcomes import stored_finding_states

    states = stored_finding_states(data, [f.get("finding_id") for f in findings])
    return [
        _compact(f, states.get(f.get("finding_id"), (None, None))[0]) for f in findings
    ]


# A page of summaries fits result_cap once rendered (#3276): the schemas cap limit at
# PAGE_LIMIT, and a page stops before its compact JSON passes PAGE_CHARS, so the
# counts and paging beside it are read whole. has_more and next_offset describe
# the entries returned, not the limit asked for.
PAGE_LIMIT = 25
PAGE_CHARS = 12_000


def _page(data: Any, findings: list, *, offset: int, limit: int, total: int) -> Args:
    page, used = [], 0
    for summary in _compact_page(data, findings):
        used += len(_compact_json(summary)) + 1
        if page and used > PAGE_CHARS:
            break
        page.append(summary)
    end = offset + len(page)
    more = end < total
    return {
        "offset": offset,
        "limit": limit,
        "returned": len(page),
        "has_more": more,
        **({"next_offset": end} if more else {}),
        "findings": page,
    }


def _compact_json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))


def _page_limit(limit: Any) -> int:
    try:
        return min(max(int(limit), 1), PAGE_LIMIT)
    except (TypeError, ValueError):
        return 20


# The derived states a findings read filters by (core.storage.service.FINDING_STATES).
FINDING_STATES = ("open", "dismissed", "resolved")


def _bad_state(state: Optional[str]) -> Optional[Args]:
    if state is None or state in FINDING_STATES:
        return None
    return {"error": f"state must be one of {', '.join(FINDING_STATES)}"}


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
    state: Optional[str] = None,
    cluster_id: Optional[str] = None,
    min_anomaly_score: Optional[float] = None,
    sort_by: str = "timestamp",
    sort_order: str = "desc",
    offset: int = 0,
    limit: int = 20,
) -> Args:
    if (bad := _bad_state(state)) is not None:
        return bad
    filters = {
        "severity": severity,
        "data_source": data_source,
        "state": state,
        "cluster_id": cluster_id,
        "min_anomaly_score": min_anomaly_score,
    }
    limit = _page_limit(limit)
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
        **_page(data, findings, offset=offset, limit=limit, total=total),
    }


# The counts cover every match, not the page, and come before it so a capped
# result keeps them (#2312).
def search_findings(**args: Any) -> Args:
    filters = {key: args.get(key) for key in ("severity", "data_source", "state")}
    if (bad := _bad_state(filters["state"])) is not None:
        return bad
    filters["search_query"] = args.get("query", "")
    limit = _page_limit(args.get("limit", 20))
    offset = args.get("offset", 0)
    data = _data()
    counts = data.get_findings_summary(**filters, top_ips=TOP_IPS)
    total = counts.pop("total")
    findings = data.get_findings(
        limit=limit,
        offset=offset,
        sort_by=args.get("sort_by", "anomaly_score"),
        sort_order=args.get("sort_order", "desc"),
        **filters,
    )
    return {
        "query": filters["search_query"],
        "total": total,
        **counts,
        **_page(data, findings, offset=offset, limit=limit, total=total),
    }


def get_findings_stats(**_args: Any) -> Args:
    summary = _data().get_findings_summary()
    return {"total_findings": summary.pop("total"), **summary}


# What an agent reads of one finding (#3276): the small fields and the stored
# triage first, entity_context last, its flow preview cut to a sample, so the
# rendered result fits result_cap. The REST endpoints still serve it whole.
SAMPLE_RECORDS = 5
SAMPLE_RAW_TEXT = 2_000
SAMPLE_PORT_RANGES = 16
_HEAD = (
    "finding_id",
    "title",
    "severity",
    "data_source",
    "timestamp",
    "created_at",
    "updated_at",
    "anomaly_score",
    "cluster_id",
    "external_id",
    "description",
    "mitre_predictions",
)
_TRIAGE_KEYS = (
    "ai_triage",
    "ai_triage_error",
    "triage_confidence",
    "category",
    "recommended_action",
    "triage_reasoning",
)


def get_finding(*, finding_id: str) -> Any:
    finding = _data().get_finding(finding_id)
    return _for_agent(finding) if isinstance(finding, dict) else finding


def _for_agent(finding: Args) -> Args:
    enrichment = finding.get("ai_enrichment")
    enrichment = dict(enrichment) if isinstance(enrichment, dict) else enrichment
    shaped: Args = {key: finding.get(key) for key in _HEAD if key in finding}
    if isinstance(enrichment, dict) and (triage := _triage(enrichment)):
        shaped["triage"] = triage
        for key in _TRIAGE_KEYS:
            enrichment.pop(key, None)
    rest = {k: v for k, v in finding.items() if k not in _HEAD}
    rest.pop("entity_context", None)
    rest.pop("ai_enrichment", None)
    shaped.update(rest)
    if enrichment:
        shaped["ai_enrichment"] = enrichment
    if "entity_context" in finding:
        shaped["entity_context"] = _sampled_context(finding["entity_context"])
    return shaped


# The daemon's triage as one labelled block, rather than scattered ai_enrichment keys.
def _triage(enrichment: Args) -> Optional[Args]:
    stored = enrichment.get("ai_triage")
    result = stored.get("result") if isinstance(stored, dict) else None
    triage = dict(result) if isinstance(result, dict) else {}
    for key, name in (
        ("triage_confidence", "confidence"),
        ("category", "category"),
        ("recommended_action", "recommended_action"),
        ("triage_reasoning", "reasoning"),
    ):
        if enrichment.get(key) is not None:
            triage.setdefault(name, enrichment[key])
    if isinstance(stored, dict) and stored.get("timestamp"):
        triage["triaged_at"] = stored["timestamp"]
    if enrichment.get("ai_triage_error"):
        triage["error"] = enrichment["ai_triage_error"]
    return triage or None


def _sampled_context(context: Any) -> Any:
    if not isinstance(context, dict):
        return context
    context = dict(context)
    evidence = context.get("source_evidence")
    if isinstance(evidence, dict):
        evidence = dict(evidence)
        records = evidence.get("records")
        if isinstance(records, list) and len(records) > SAMPLE_RECORDS:
            evidence["records"] = records[:SAMPLE_RECORDS]
            evidence["records_shown"] = (
                f"{SAMPLE_RECORDS} of the {len(records)} stored; "
                "netflow_digest counts every flow"
                if "netflow_digest" in context
                else f"{SAMPLE_RECORDS} of the {len(records)} stored"
            )
        raw = evidence.get("raw_text")
        if isinstance(raw, str) and len(raw) > SAMPLE_RAW_TEXT:
            evidence["raw_text"] = raw[:SAMPLE_RAW_TEXT]
            evidence["raw_text_shown"] = f"{SAMPLE_RAW_TEXT} of {len(raw)} characters"
        # Last, so whatever still overflows is cut from the preview.
        context.pop("source_evidence")
        context["source_evidence"] = evidence
    digest = context.get("netflow_digest")
    if isinstance(digest, dict) and isinstance(digest.get("pairs"), list):
        # A pair's port ranges run to 256 entries; port_count and top_ports stay.
        digest = dict(digest)
        digest["pairs"] = [
            (
                {k: v for k, v in pair.items() if k != "port_ranges"}
                if isinstance(pair, dict)
                and len(pair.get("port_ranges") or ()) > SAMPLE_PORT_RANGES
                else pair
            )
            for pair in digest["pairs"]
        ]
        context["netflow_digest"] = digest
    return context


# A dismissal is the finding's terminal state. The actor is the caller and the
# kind is agent, as with a status edit that closes a case; the reason is what makes it a record.
def dismiss_finding(*, finding_id: str, reason: str) -> Args:
    if refusal := permission_refusal("findings.write"):
        return refusal
    from core.cases.agent_closure import actor, service_session
    from core.cases.closure import ClosedByKind
    from core.storage.models import Finding
    from core.time import utcnow

    reason = (reason or "").strip()
    if not reason:
        return {"error": "A reason is required to dismiss a finding"}
    with service_session() as session:
        finding = session.get(Finding, finding_id)
        if finding is None:
            return {"error": f"Finding {finding_id} not found"}
        finding.noise_marked_at = utcnow()
        finding.noise_marked_by = actor()
        finding.noise_marked_by_kind = ClosedByKind.AGENT.value
        finding.noise_reason = reason
        return {
            "success": True,
            "finding_id": finding_id,
            "noise_marked_by": finding.noise_marked_by,
            "noise_marked_by_kind": finding.noise_marked_by_kind,
            "noise_reason": reason,
        }


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


def _status_refusal(status: str) -> Optional[Args]:
    if status in CASE_STATUSES:
        return None
    return {
        "error": f"Unknown case status {status!r}; use one of {list(CASE_STATUSES)}"
    }


def _resolve_assignee(
    raw: str, current: Optional[str] = None
) -> Tuple[Optional[str], Optional[Args]]:
    """The username ``raw`` names, or the refusal to hand back to the agent."""
    from core.cases.agent_closure import service_session
    from core.cases.assignee import AssigneeRefused, resolve_assignee

    try:
        with service_session() as session:
            return resolve_assignee(session, raw, current=current), None
    except AssigneeRefused as exc:
        return None, {"error": exc.message}


def create_case(
    *,
    title: str,
    description: str = "",
    severity: Optional[str] = None,
    priority: Optional[str] = None,
    finding_ids: Optional[list] = None,
    status: CaseStatus = "new",
    assignee: Optional[str] = None,
    tags: Optional[list] = None,
) -> Args:
    if refusal := permission_refusal("cases.write"):
        return refusal
    if assignee is not None and (refusal := permission_refusal("cases.assign")):
        return refusal
    if refusal := _status_refusal(status):
        return refusal
    if assignee is not None:
        # Before the case is opened, so a refusal leaves no case behind.
        assignee, refusal = _resolve_assignee(assignee)
        if refusal:
            return refusal
    data = _data()
    ids = finding_ids or []
    case = open_case(
        data,
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
    status: Optional[CaseStatus] = None,
    priority: Optional[str] = None,
    assignee: Optional[str] = None,
    add_note: Optional[str] = None,
) -> Args:
    if refusal := permission_refusal("cases.write"):
        return refusal
    if status is not None and (refusal := _status_refusal(status)):
        return refusal
    from core.cases.agent_closure import actor, service_session
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
    if status is not None:
        updates["status"] = status
    if priority:
        updates["priority"] = priority
    if assignee is not None:
        if refusal := permission_refusal("cases.assign"):
            return refusal
        # Blank unassigns; re-sending the stored value keeps it.
        updates["assignee"], refusal = _resolve_assignee(
            assignee, current=case.get("assignee")
        )
        if refusal:
            return refusal
    if add_note:
        notes = list(case.get("notes") or [])
        notes.append({"timestamp": utcnow().isoformat() + "Z", "content": add_note})
        updates["notes"] = notes

    change = status_change(case.get("status"), status)
    # The write and a close share one session, so a refused close leaves the
    # case as it was. A write with no close owns its own session.
    with (service_session() if change else nullcontext()) as session:
        if not data.update_case(case_id, session=session, **updates):
            return {"error": "Failed to update case"}
        if change:
            record_status_change(
                session,
                case_id,
                change,
                closed_by=actor(),
            )
    return {"success": True, "case_id": case_id}


# The case's findings ride beside its tasks and escalations, paged: under
# "findings", not an envelope key tools_router would unwrap and drop siblings of.
def _case_records(args: Args) -> Args:
    from sqlalchemy import func
    from sqlalchemy.orm import noload

    from core.cases.case_records_service import list_escalations, tasks_on_case
    from core.storage.models import Finding, case_findings
    from core.storage.schemas import FindingSchema
    from core.storage.schemas.case_entities import CaseEscalationSchema, CaseTaskSchema
    from core.storage.unit_of_work import unit_of_work

    case_id = str(args.get("case_id") or "")
    offset = max(int(args.get("offset") or 0), 0)
    limit = max(int(args.get("limit") or 100), 1)
    with unit_of_work() as session:
        on_case = (
            session.query(Finding)
            .join(case_findings, case_findings.c.finding_id == Finding.finding_id)
            .filter(case_findings.c.case_id == case_id)
        )
        total = on_case.with_entities(func.count()).scalar() or 0
        page = (
            on_case.options(
                noload(Finding.cases), noload(Finding.mitre_prediction_rows)
            )
            .order_by(Finding.timestamp.desc().nulls_last(), Finding.finding_id)
            .offset(offset)
            .limit(limit)
            .all()
        )
        records = {
            "tasks": CaseTaskSchema.dump_many(tasks_on_case(session, case_id)),
            "escalations": CaseEscalationSchema.dump_many(
                list_escalations(session, case_id)
            ),
        }
        flows = _case_flows(session, on_case)
        findings = FindingSchema.dump_many(page)
    if flows is not None:
        # Ahead of the findings page, so a cut at result_cap takes findings.
        records["flows"] = flows
    return {
        **records,
        "total": total,
        **_page(_data(), findings, offset=offset, limit=limit, total=total),
    }


# Every NetFlow finding on the case, not the page: its ingest digest, read by
# JSON path so the flow previews stay in the database (#3275).
def _case_flows(session: Any, on_case: Any) -> Optional[Args]:
    from core.findings.netflow_digest import case_flow_summary
    from core.storage.models import Finding

    rows = on_case.with_entities(
        Finding.finding_id,
        Finding.entity_context["netflow_digest"],
        Finding.entity_context["source_evidence"]["telemetry_kind"],
    ).all()
    digested = [(fid, digest) for fid, digest, _ in rows if digest is not None]
    missing = [
        fid for fid, digest, kind in rows if digest is None and kind == "netflow"
    ]
    if not digested and not missing:
        return None
    return case_flow_summary(digested, without_digest=missing)


def add_finding_to_case(*, case_id: str, finding_id: str) -> Args:
    if refusal := permission_refusal("cases.write"):
        return refusal
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
    if refusal := permission_refusal("cases.write"):
        return refusal
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


# A whole replay carries two digests per decision and ran to ~450,000 characters,
# which the tool result cap truncated to nothing useful. The tool answers a
# bounded summary of a step or range instead: no digests, the latest steps when
# none is asked for, and a hard ceiling on the text either way.
REPLAY_MAX_STEPS = 5
REPLAY_MAX_RECALLED = 5
REPLAY_NOTE_CHARS = 200
REPLAY_MAX_CHARS = 8_000


def _step(value: Any, name: str) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise MCPFailure(
            "invalid_args", f"replay_hunt {name} must be a whole step number"
        ) from None


def _clip(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _replay_summary(report: Args, first: Optional[int], last: Optional[int]) -> Args:
    decisions = [d for d in report.get("decisions") or [] if isinstance(d, dict)]
    if first is None and last is None:
        picked = decisions[-REPLAY_MAX_STEPS:]
        window = "latest steps"
    else:
        lo = first if first is not None else 0
        picked = [
            d
            for d in decisions
            if lo
            <= int(d.get("iteration") or 0)
            <= (last if last is not None else 10**9)
        ]
        window = f"steps {lo} to {last if last is not None else 'the end'}"
    omitted = max(len(picked) - REPLAY_MAX_STEPS, 0)
    picked = picked[:REPLAY_MAX_STEPS]
    steps = [
        {
            "step": d.get("iteration"),
            "decision_id": d.get("decision_id"),
            "action": d.get("action"),
            "target": d.get("target"),
            "cost_usd": d.get("cost_usd"),
            "reproduced": d.get("mismatch") is None,
            "mismatch": _clip(d.get("mismatch"), REPLAY_NOTE_CHARS) or None,
            "exact": d.get("exact"),
        }
        for d in picked
    ]
    recalled = [str(n) for n in report.get("recalled") or []]
    summary: Args = {
        "hunt_id": report.get("hunt_id"),
        "window": window,
        "steps_in_run": len(decisions),
        "steps": steps,
        "recalled": [
            _clip(n, REPLAY_NOTE_CHARS) for n in recalled[:REPLAY_MAX_RECALLED]
        ],
    }
    if omitted:
        summary["omitted_steps"] = (
            f"{omitted} more steps matched; ask for a narrower step or range"
        )
    if len(recalled) > REPLAY_MAX_RECALLED:
        summary["omitted_recalled"] = len(recalled) - REPLAY_MAX_RECALLED
    # Shed from the oldest end until it fits, saying so rather than cutting a row.
    while len(json.dumps(summary, default=str)) > REPLAY_MAX_CHARS and summary["steps"]:
        summary["steps"] = summary["steps"][1:]
        summary["omitted_steps"] = (
            "output limit reached; ask for a narrower step or range"
        )
    return summary


# None from the read is "nothing to replay"; a model needs a body, not null.
async def replay_hunt(
    *,
    run_id: str,
    decision_id: Optional[str] = None,
    step: Any = None,
    from_step: Any = None,
    to_step: Any = None,
) -> Any:
    if not run_id:
        raise MCPFailure(
            "invalid_args", "replay_hunt is missing a required argument: run_id"
        )
    first, last = _step(from_step, "from_step"), _step(to_step, "to_step")
    only = _step(step, "step")
    if only is not None:
        first = last = only
    report = await read_replay(str(run_id), decision_id or None)
    if report is None:
        return {"error": f"Nothing to replay for run {run_id}"}
    return _replay_summary(report, first, last)


_SECURITY_TOOLS = frozenset(
    {
        "analyze_coverage",
        "search_detections",
        "identify_gaps",
        "get_coverage_stats",
        "get_detection_count",
        "lint_detections",
        "check_detection_candidate",
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
        raise MCPFailure(
            "invalid_args", "lookup_indicators is missing a required argument: values"
        )

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
        raise MCPFailure("invalid_args", str(exc)) from None


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


# Chat and MCP do not approve. The row stays as it is; a person confirms it
# on the Approvals screen. A run no person started passes permission_refusal,
# and this does not add a second denial for that case.
def approve_action(*, action_id: str) -> Args:
    if refusal := permission_refusal(APPROVE_PERMISSION):
        return refusal
    action = _approvals().get_action(action_id)
    if action is None:
        return {"error": f"Action {action_id} was not found"}
    return {
        "action_id": action.action_id,
        "status": action.status,
        "message": "Confirm this action in Approvals. Chat cannot approve it.",
    }


def reject_action(*, action_id: str, reason: str) -> Args:
    from core.cases.agent_closure import actor
    from core.response.approval_service import AlreadyDecided

    if refusal := permission_refusal(APPROVE_PERMISSION):
        return refusal
    claims = current_claims()
    principal = None if claims is None else (claims.person_id or claims.agent_id)
    try:
        action = _approvals().reject_action(
            action_id, reason, actor(), approved_by_principal_id=principal
        )
    except AlreadyDecided as e:
        return {"error": str(e)}
    return _decided(action, "rejected")


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
        "dismiss_finding",
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
    registry: Optional["MCPRegistry"] = None,
) -> Tuple[Any, bool]:
    args = dict(tool_input or {})
    # permission_refusal reads the permissions-module binding. A caller bound to
    # nothing is refused.
    permission = TOOL_PERMISSIONS.get(tool_name)
    if permission and (refusal := permission_refusal(permission)):
        return refusal, True

    if tool_name == FIND_INTEGRATION_TOOLS and registry is not None:
        from core.agents.integration_tools import find_integration_tools

        return find_integration_tools(registry, **args), True

    if tool_name == "case_records":
        return _case_records(args), True

    if tool_name in _OWNED:
        result = globals()[tool_name](**args)
        if inspect.isawaitable(result):
            result = await result
        return result, True

    if tool_name in _SECURITY_TOOLS:
        from core.detections.tools import get_security_detection_tools

        return await getattr(get_security_detection_tools(), tool_name)(**args), True

    if tool_name in _INTEL_TOOLS:
        return _INTEL_TOOLS[tool_name](args), True

    if tool_name in _MEMORY_TOOLS:
        result = _MEMORY_TOOLS[tool_name](args)
        if inspect.isawaitable(result):
            result = await result
        return result, True

    # Agent skills (#925): reads from disk only, never a database.
    if tool_name == READ_SKILL_TOOL:
        return read_skill(args.get("name"), args.get("file")), True

    return None, False
