"""The Overview read: arrivals, outcome nodes, agent rows, and the feed."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from typing import Optional

from core.findings.alert_outcomes import (
    TERMINAL_LABELS,
    _cases_for,
    terminal_states_for,
    terminal_states_today,
)
from core.findings.arrival_counts import arrivals_today_by_source
from core.findings.source_evidence import project_finding_source_evidence_for_list
from core.findings.source_link import resolve_source_link
from core.storage.connection import get_db_manager
from core.storage.models import FederationSource, Finding, WorkflowRun, WorkflowRunPhase
from core.time import utcnow
from core.workflows.workflows_service import WorkflowsService

FEED_LIMIT = 50
RATE_WINDOW = timedelta(days=30)
GOOD_AT = 0.95
FAIR_AT = 0.85
# budget_exhausted is stored as completed (core/workflows/run_bridge_router.py).
RATE_INFO = (
    "Completed divided by completed, failed, and cancelled runs started in "
    "the last 30 days. Good at or above 95%, fair from 85% up to 95%, poor "
    "below that. A sample of zero has no level. Rows hidden from history "
    "still count. A run that stopped at its budget counts as completed, "
    "because that is how the row is stored."
)
ARRIVAL_SOURCE = "Findings created today (UTC) with this data source."
OUTCOME_SOURCE = "Alerts that arrived today (UTC) and reached this state. Each alert is counted once."
ENGINE_SOURCE = "No count. The outcome counts partition today's arrivals."
RUNNING_SOURCE = "Runs still in history whose status is running or paused."
STEP_SOURCE = "The open phase on the newest live run, or that run's status when it has no phase row."
UNMEASURED_TEXT = "Not measured yet"
NEEDS_YOU_INFO = "This count is alerts, not decisions."

# Ticket, dropped, paused, stuck, and incidents stay outside the sum.
_UNMEASURED = (
    (
        "ticket",
        "Ticket created",
        "Jira export returns an issue key and does not store it.",
    ),
    (
        "dropped",
        "Dropped as noise",
        "There is no score floor, and a noise mark is not a disposition.",
    ),
    (
        "paused",
        "Paused",
        "There is no paused ledger. A run waiting on approval is not paused.",
    ),
    ("stuck", "Stuck", "There is no stuck ledger."),
    ("incidents", "Incidents", "Incidents are not measured."),
)
_LIVE_ORDER = (
    "resolved_auto",
    "resolved_person",
    "working",
    "needs_you",
    "waiting",
)
_TERMINAL_STATUSES = ("completed", "failed", "cancelled")
_LIVE_STATUSES = ("running", "paused")
_OPEN_PHASE = ("running", "pending_approval")


def completion_level(sample: int, completed: int) -> Optional[str]:
    if sample <= 0:
        return None
    rate = completed / sample
    if rate >= GOOD_AT:
        return "good"
    if rate >= FAIR_AT:
        return "fair"
    return "poor"


def _outcome_nodes(states: list[dict]) -> list[dict]:
    counts = Counter(row["terminal_state"] for row in states)
    nodes = []
    for state in _LIVE_ORDER:
        nodes.append(
            {
                "state": state,
                "label": TERMINAL_LABELS[state],
                "count": counts.get(state, 0),
                "source_text": OUTCOME_SOURCE,
                "info": NEEDS_YOU_INFO if state == "needs_you" else None,
                "unmeasured_text": None,
            }
        )
    for state, label, info in _UNMEASURED:
        nodes.append(
            {
                "state": state,
                "label": label,
                "count": None,
                "source_text": info,
                "info": info,
                "unmeasured_text": UNMEASURED_TEXT,
            }
        )
    return nodes


def _agent_rows(now: datetime) -> list[dict]:
    workflows = WorkflowsService().list_workflows()
    since = now - RATE_WINDOW
    db = get_db_manager()
    with db.session_scope() as session:
        grouped = (
            session.query(WorkflowRun.workflow_id, WorkflowRun.status)
            .filter(
                WorkflowRun.started_at >= since,
                WorkflowRun.status.in_(_TERMINAL_STATUSES),
            )
            .all()
        )
        live = (
            session.query(WorkflowRun)
            .filter(
                WorkflowRun.status.in_(_LIVE_STATUSES),
                WorkflowRun.deleted_at.is_(None),
            )
            .all()
        )
        newest: dict[str, WorkflowRun] = {}
        running: Counter[str] = Counter()
        for run in live:
            running[run.workflow_id] += 1
            current = newest.get(run.workflow_id)
            if current is None or (run.started_at, run.run_id) > (
                current.started_at,
                current.run_id,
            ):
                newest[run.workflow_id] = run
        phases: dict[str, WorkflowRunPhase] = {}
        if newest:
            open_phases = (
                session.query(WorkflowRunPhase)
                .filter(
                    WorkflowRunPhase.run_id.in_(
                        [run.run_id for run in newest.values()]
                    ),
                    WorkflowRunPhase.status.in_(_OPEN_PHASE),
                )
                .all()
            )
            for phase in open_phases:
                current = phases.get(phase.run_id)
                if current is None or phase.phase_order > current.phase_order:
                    phases[phase.run_id] = phase
        # Detach the bits we need before the session closes.
        steps = {}
        for workflow_id, run in newest.items():
            phase = phases.get(run.run_id)
            steps[workflow_id] = phase.phase_id if phase is not None else run.status

    by_status: dict[str, Counter[str]] = {}
    for workflow_id, status in grouped:
        by_status.setdefault(workflow_id, Counter())[status] += 1

    rows = []
    for workflow in workflows:
        workflow_id = workflow["id"]
        tally = by_status.get(workflow_id, Counter())
        completed = tally.get("completed", 0)
        failed = tally.get("failed", 0)
        cancelled = tally.get("cancelled", 0)
        sample = completed + failed + cancelled
        rows.append(
            {
                "workflow_id": workflow_id,
                "name": workflow.get("name") or workflow_id,
                "running": running.get(workflow_id, 0),
                "sample_size": sample,
                "rate": None if sample == 0 else completed / sample,
                "level": completion_level(sample, completed),
                "current_step": steps.get(workflow_id),
            }
        )
    rows.sort(key=lambda row: (row["name"] or "", row["workflow_id"]))
    return rows


def _feed() -> list[dict]:
    db = get_db_manager()
    with db.session_scope() as session:
        findings = (
            session.query(Finding)
            .filter(Finding.noise_marked_at.is_(None))
            .order_by(Finding.created_at.desc(), Finding.finding_id.desc())
            .limit(FEED_LIMIT)
            .all()
        )
        staged = []
        for finding in findings:
            projected = project_finding_source_evidence_for_list(
                {"entity_context": finding.entity_context}
            )
            context = projected.get("entity_context")
            evidence = (
                context.get("source_evidence") if isinstance(context, dict) else None
            )
            created = finding.created_at.isoformat() if finding.created_at else None
            staged.append(
                {
                    "finding_id": finding.finding_id,
                    "severity": finding.severity,
                    "data_source": finding.data_source,
                    "external_id": finding.external_id,
                    "status": finding.status,
                    "description": finding.description,
                    "created_at": created,
                    "evidence_links": list(finding.evidence_links or []),
                    "source_evidence": evidence,
                }
            )
        linked = _cases_for(session, [row["finding_id"] for row in staged])
    link_configs: dict[str, dict[str, str]] = {}
    items = []
    for row in staged:
        external_id = row.pop("external_id")
        case = linked.get(row["finding_id"])
        items.append(
            {
                **row,
                "source_link": resolve_source_link(
                    {
                        "evidence_links": row["evidence_links"],
                        "data_source": row["data_source"],
                        "external_id": external_id,
                    },
                    configs=link_configs,
                ),
                "case_id": case.case_id if case else None,
            }
        )
    states = terminal_states_for([item["finding_id"] for item in items])
    for item in items:
        state = states[item["finding_id"]]
        item["terminal_state"] = state
        item["terminal_label"] = TERMINAL_LABELS[state]
    return items


def overview_payload(
    day: Optional[date] = None, now: Optional[datetime] = None
) -> dict:
    """One payload for the Overview screen. ``day`` defaults to the UTC day."""
    if day is None:
        day = utcnow().date()
    if now is None:
        now = utcnow()
    arrivals = [
        {**row, "source_text": ARRIVAL_SOURCE} for row in arrivals_today_by_source(day)
    ]
    states = terminal_states_today(day)
    arrival_total = sum(row["count"] for row in arrivals)
    db = get_db_manager()
    with db.session_scope() as session:
        enabled_count = (
            session.query(FederationSource)
            .filter(FederationSource.enabled.is_(True))
            .count()
        )
    return {
        "day": day.isoformat(),
        "empty": enabled_count == 0 and arrival_total == 0,
        "arrivals": arrivals,
        "engine": {"source_text": ENGINE_SOURCE},
        "outcomes": _outcome_nodes(states),
        "running_source": RUNNING_SOURCE,
        "step_source": STEP_SOURCE,
        "rate_info": RATE_INFO,
        "good_at": GOOD_AT,
        "fair_at": FAIR_AT,
        "agents": _agent_rows(now),
        "feed": _feed(),
    }
