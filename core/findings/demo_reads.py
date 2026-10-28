"""Case, overview, triage, and needs-you reads over demo rows.

Demo mode keeps findings and cases in memory. These screens used to read
Postgres anyway, so the banner said the data was demo while the pages showed
the database, or nothing. None of this opens a database connection.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional

from core.cases.combined_state import QueueItem
from core.cases.linked_entities import finding_entities
from core.findings.source_evidence import project_finding_source_evidence_for_list
from core.findings.source_link import resolve_source_link
from core.platform.demo_data_service import DemoDataService

_EPOCH = datetime(1970, 1, 1)
_CLOSED = "closed"


def load_demo() -> tuple[list[dict], list[dict]]:
    service = DemoDataService()
    return service.get_findings(10000), service.get_cases(10000)


def parse_utc(value: object) -> Optional[datetime]:
    """Naive UTC, matching the timestamps the database reads return."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _ord(value: Optional[datetime]) -> float:
    if value is None:
        return float("-inf")
    return (value - _EPOCH).total_seconds()


def _ids(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str) and item]


def _text(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def best_case_by_finding(cases: list[dict]) -> dict[str, dict]:
    """Newest case that links each finding. Tie-break is the case id."""
    best: dict[str, tuple[tuple, dict]] = {}
    for case in cases:
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            continue
        updated = parse_utc(case.get("updated_at")) or parse_utc(case.get("created_at"))
        rank = (_ord(updated), case_id)
        for finding_id in _ids(case.get("finding_ids")):
            current = best.get(finding_id)
            if current is None or rank > current[0]:
                best[finding_id] = (rank, case)
    return {finding_id: pair[1] for finding_id, pair in best.items()}


def terminal_state(case: Optional[dict], *, dismissed: bool = False) -> str:
    """The same partition the live read uses, for rows that have no live run.

    A dismissal wins. Every non-closed demo case is on the needs-you list, so
    its findings are needs-you rather than working: nothing is actually running.
    A closed case has no closure row, which counts as an agent close.
    """
    if dismissed:
        return "dismissed"
    if case is None:
        return "waiting"
    if (case.get("status") or "") == _CLOSED:
        return "resolved_auto"
    return "needs_you"


def open_case_ids(cases: list[dict]) -> set[str]:
    """Demo cases that are not closed. Nothing is investigating them for real."""
    return {
        case_id
        for case in cases
        if (case_id := case.get("case_id"))
        and isinstance(case_id, str)
        and (case.get("status") or "") != _CLOSED
    }


def _when(finding: dict) -> Optional[datetime]:
    return parse_utc(finding.get("timestamp") or finding.get("created_at"))


def _noise(finding: dict) -> bool:
    return bool(finding.get("noise_marked") or finding.get("noise_marked_at"))


def _evidence(finding: dict) -> Optional[dict]:
    projected = project_finding_source_evidence_for_list(
        {"entity_context": finding.get("entity_context")}
    )
    context = projected.get("entity_context")
    if not isinstance(context, dict):
        return None
    evidence = context.get("source_evidence")
    return evidence if isinstance(evidence, dict) else None


def feed_item(finding: dict, case: Optional[dict], *, link_configs: dict) -> dict:
    when = _when(finding)
    data_source = (
        finding.get("data_source")
        if isinstance(finding.get("data_source"), str)
        else ""
    )
    title = finding.get("title") if isinstance(finding.get("title"), str) else None
    description = (
        finding.get("description")
        if isinstance(finding.get("description"), str)
        else None
    )
    return {
        "finding_id": finding.get("finding_id"),
        "severity": finding.get("severity"),
        "data_source": data_source,
        "title": title,
        "description": description,
        "created_at": when.isoformat() if when else None,
        "source_evidence": _evidence(finding),
        "source_link": resolve_source_link(
            {
                "evidence_links": finding.get("evidence_links") or [],
                "data_source": data_source,
                "external_id": finding.get("external_id"),
            },
            configs=link_configs,
        ),
        "case_id": case.get("case_id") if case else None,
        "terminal_state": terminal_state(case, dismissed=_noise(finding)),
        "noise_marked": _noise(finding),
    }


def overview_parts(
    findings: list[dict],
    cases: list[dict],
    *,
    day,
    feed_limit: int,
) -> dict:
    """Arrivals, today's states, and the feed. Agents and outcome nodes stay with Overview."""
    linked = best_case_by_finding(cases)
    arrivals: dict[str, int] = {}
    states = []
    for finding in findings:
        when = _when(finding)
        if when is None or when.date() != day:
            continue
        source = (
            finding.get("data_source")
            if isinstance(finding.get("data_source"), str)
            else ""
        )
        arrivals[source] = arrivals.get(source, 0) + 1
        case = linked.get(finding.get("finding_id"))
        states.append(
            {
                "finding_id": finding.get("finding_id"),
                "data_source": source,
                "terminal_state": terminal_state(case, dismissed=_noise(finding)),
            }
        )
    configs: dict = {}
    ranked = sorted(
        (finding for finding in findings if not _noise(finding)),
        key=lambda finding: (
            _when(finding) is not None,
            _ord(_when(finding)),
            finding.get("finding_id") or "",
        ),
        reverse=True,
    )
    feed = [
        feed_item(finding, linked.get(finding.get("finding_id")), link_configs=configs)
        for finding in ranked[:feed_limit]
    ]
    return {
        "empty": not findings,
        "arrivals": [
            {"data_source": name, "count": arrivals[name]} for name in sorted(arrivals)
        ],
        "states": states,
        "feed": feed,
    }


def overview_alert(
    findings: list[dict], cases: list[dict], finding_id: str
) -> Optional[dict]:
    linked = best_case_by_finding(cases)
    for finding in findings:
        if finding.get("finding_id") == finding_id:
            return feed_item(finding, linked.get(finding_id), link_configs={})
    return None


def linked_findings(finding_ids: object, findings: list[dict]) -> list[dict]:
    """Case detail rows for ids the case already stores, in that order."""
    by_id = {
        finding["finding_id"]: finding
        for finding in findings
        if isinstance(finding.get("finding_id"), str)
    }
    configs: dict = {}
    rows = []
    for finding_id in _ids(finding_ids):
        finding = by_id.get(finding_id)
        if finding is None:
            continue
        data_source = (
            finding.get("data_source")
            if isinstance(finding.get("data_source"), str)
            else ""
        )
        rows.append(
            {
                "finding_id": finding_id,
                "title": (
                    finding.get("title")
                    if isinstance(finding.get("title"), str)
                    else None
                ),
                "description": (
                    finding.get("description")
                    if isinstance(finding.get("description"), str)
                    else None
                ),
                "entities": finding_entities(finding.get("entity_context")),
                "source_link": resolve_source_link(
                    {
                        "evidence_links": finding.get("evidence_links") or [],
                        "data_source": data_source,
                        "external_id": finding.get("external_id"),
                    },
                    configs=configs,
                ),
            }
        )
    return rows


def _matches(
    case: dict,
    *,
    findings_by_id: dict[str, dict],
    query: Optional[str],
    priority: Optional[str],
    assignee: Optional[str],
    state: Optional[str],
    closed: Optional[bool],
    sla_at_risk: bool,
    needs_you_only: bool,
    needs_you_ids: set[str],
    workflow_constrained: bool,
    data_source: Optional[str],
) -> bool:
    # Demo cases have no workflow and no SLA, so those filters keep nothing.
    if workflow_constrained or sla_at_risk:
        return False
    status = (case.get("status") or "").strip()
    if closed is True:
        if status != _CLOSED:
            return False
    elif state:
        if status != state:
            return False
    elif status == _CLOSED:
        return False
    case_id = case.get("case_id") or ""
    if needs_you_only and case_id not in needs_you_ids:
        return False
    if priority and (case.get("priority") or "") != priority:
        return False
    if assignee is not None and (case.get("assignee") or "") != assignee:
        return False
    if data_source:
        sources = {
            findings_by_id.get(finding_id, {}).get("data_source")
            for finding_id in _ids(case.get("finding_ids"))
        }
        if data_source not in sources:
            return False
    if query:
        haystack = " ".join(
            str(case.get(field) or "")
            for field in ("title", "description", "case_id", "assignee")
        ).lower()
        if query.lower() not in haystack:
            return False
    return True


def case_queue(
    cases: list[dict],
    findings: list[dict],
    *,
    now: datetime,
    limit: int,
    offset: int,
    query: Optional[str] = None,
    priority: Optional[str] = None,
    assignee: Optional[str] = None,
    state: Optional[str] = None,
    closed: Optional[bool] = None,
    sla_at_risk: bool = False,
    needs_you_only: bool = False,
    workflow_constrained: bool = False,
    data_source: Optional[str] = None,
) -> dict:
    """One page of demo cases, in the queue's shape. The strip ignores the filters."""
    needs = open_case_ids(cases)
    findings_by_id = {
        finding["finding_id"]: finding
        for finding in findings
        if isinstance(finding.get("finding_id"), str)
    }
    matched = [
        case
        for case in cases
        if _matches(
            case,
            findings_by_id=findings_by_id,
            query=query,
            priority=priority,
            assignee=assignee,
            state=state,
            closed=closed,
            sla_at_risk=sla_at_risk,
            needs_you_only=needs_you_only,
            needs_you_ids=needs,
            workflow_constrained=workflow_constrained,
            data_source=data_source,
        )
    ]
    matched.sort(
        key=lambda case: (
            0 if (case.get("case_id") or "") in needs else 1,
            -_ord(
                parse_utc(case.get("updated_at")) or parse_utc(case.get("created_at"))
            ),
            case.get("case_id") or "",
        )
    )
    page = matched[offset : offset + limit]
    items = []
    for case in page:
        case_id = case.get("case_id") or ""
        created = parse_utc(case.get("created_at"))
        updated = parse_utc(case.get("updated_at")) or created
        age = (now - created).total_seconds() if created else 0.0
        assignee_value = (
            case.get("assignee") if isinstance(case.get("assignee"), str) else ""
        )
        description = case.get("description")
        items.append(
            asdict(
                QueueItem(
                    case_id=case_id,
                    title=case.get("title") or case_id,
                    priority=case.get("priority"),
                    assignee=assignee_value or None,
                    combined_state=(case.get("status") or "open").strip() or "open",
                    workflow_id=None,
                    findings_count=len(_ids(case.get("finding_ids"))),
                    iteration_count=None,
                    cost_usd=None,
                    max_cost_usd=None,
                    budget_health=None,
                    comment_count=0,
                    last_activity=updated,
                    age_seconds=max(age, 0.0),
                    sla_seconds_left=None,
                    health_status=None,
                    needs_you=case_id in needs,
                    description=description if isinstance(description, str) else None,
                    sla_paused=False,
                )
            )
        )
    by_state: dict[str, int] = {}
    for case in cases:
        status = (case.get("status") or "").strip()
        if not status or status == _CLOSED:
            continue
        by_state[status] = by_state.get(status, 0) + 1
    return {
        "cases": items,
        "total": len(matched),
        "limit": limit,
        "offset": offset,
        "has_more": offset + len(page) < len(matched),
        "strip": {
            "by_state": by_state,
            "sla_at_risk": 0,
            "closed_today": 0,
            "agent_closure_share": 0.0,
            "needs_you": sum(by_state.values()),
        },
    }


def needs_you_items(cases: list[dict], case_id: Optional[str] = None) -> dict:
    """Non-closed demo cases, oldest first. Home opens the case; nothing is approved."""
    wanted = _text(case_id)
    rows = []
    for case in cases:
        if (case.get("status") or "") == _CLOSED:
            continue
        current = case.get("case_id")
        if not isinstance(current, str) or not current:
            continue
        if wanted is not None and current != wanted:
            continue
        created = parse_utc(case.get("created_at"))
        description = (
            case.get("description") if isinstance(case.get("description"), str) else ""
        )
        # Same stamp the approval rows use. A zoneless value parses as local time.
        created_at = (
            "" if created is None else created.replace(tzinfo=timezone.utc).isoformat()
        )
        rows.append(
            (
                created or datetime.max,
                current,
                {
                    "kind": "open_case",
                    "source_id": current,
                    "title": case.get("title") or current,
                    "reason": description or (case.get("status") or "open"),
                    "created_at": created_at,
                    "reversibility": "reversible",
                    "case_id": current,
                    "case_ids": [current],
                    "run_id": None,
                    "checkpoint_id": None,
                    "checkpoint_class": None,
                    "hypotheses": [],
                },
            )
        )
    rows.sort(key=lambda row: (row[0], row[1]))
    items = [row[2] for row in rows]
    return {"count": len(items), "items": items}
