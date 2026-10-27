"""GET /api/overview/outcomes/{state} and /api/overview/agents/{workflow_id}.

The Overview board counts today's alerts by outcome and lists the workflows
that run. These two reads are what a click on one of them opens: the alerts
behind an outcome count (and the cases they sit in), and one workflow's steps,
live runs and recent runs. The outcome read partitions today's alerts the way
the board does, so its total is the board's count.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from core.deps import provide_workflows
from core.findings.alert_outcomes import (
    TERMINAL_LABELS,
    finding_states,
    terminal_states_today,
)
from core.routing import Auth, RouterMeta
from core.storage.connection import get_db_manager
from core.storage.models import Case, Finding, WorkflowRun
from core.time import utcnow
from core.workflows import catalog
from core.workflows.workflow_run_service import IN_FLIGHT_STATUSES

router = APIRouter()

ROUTER_META = RouterMeta(prefix="/api/overview", tags=["overview"], auth=Auth.REQUIRED)

# The board's node labels, which name the dismissed node apart from its pill.
LABELS = {**TERMINAL_LABELS, "dismissed": "Dropped as noise"}
EXPLAIN = {
    "resolved_auto": "Today's alerts whose case an agent closed without a person.",
    "resolved_person": "Today's alerts whose case an analyst closed.",
    "working": "Today's alerts in a case that is still open and has nothing waiting on a person.",
    "needs_you": "Today's alerts in a case with an approval waiting on a person.",
    "dismissed": "Today's alerts triage dropped as noise, or someone marked as noise.",
    "scoring": "Today's alerts queued for triage that have not been scored yet.",
    "waiting": "Today's alerts that are not in a case, not queued for triage and not dropped.",
}
IN_CASE = ("resolved_auto", "resolved_person", "working", "needs_you")
FINISHED = ("completed", "failed", "cancelled")
RUN_WINDOW = timedelta(days=30)
RECENT_RUNS = 12


def _iso(t: Any) -> Optional[str]:
    return t.isoformat() if hasattr(t, "isoformat") else None


def _demo_on() -> bool:
    from core.config import is_demo_mode

    return is_demo_mode()


def _empty(state: str, total: int, offset: int) -> Dict[str, Any]:
    return {
        "state": state,
        "label": LABELS[state],
        "explain": EXPLAIN[state],
        "total": total,
        "offset": offset,
        "items": [],
        "cases": [],
        "by_source": [],
    }


def _by_source(sources) -> list[dict]:
    return [{"data_source": s, "count": n} for s, n in Counter(sources).most_common()]


def _sort_cases(cases: list[dict]) -> list[dict]:
    return sorted(cases, key=lambda c: (-c["alerts"], c["updated_at"] or ""))


def _demo_outcome(state: str, limit: int, offset: int) -> Dict[str, Any]:
    """The same read over demo rows, partitioned by ``overview_parts`` as the board is."""
    from core.findings.demo_reads import (
        best_case_by_finding,
        feed_item,
        load_demo,
        overview_parts,
        parse_utc,
    )

    findings, cases = load_demo()
    parts = overview_parts(findings, cases, day=utcnow().date(), feed_limit=0)
    ids = {r["finding_id"] for r in parts["states"] if r["terminal_state"] == state}
    out = _empty(state, len(ids), offset)
    if not ids:
        return out
    linked = best_case_by_finding(cases)
    rows = [
        feed_item(f, linked.get(f.get("finding_id")), link_configs={})
        for f in findings
        if f.get("finding_id") in ids
    ]
    rows.sort(key=lambda r: r["finding_id"] or "")
    rows.sort(
        key=lambda r: (r["created_at"] is not None, parse_utc(r["created_at"]) or 0),
        reverse=True,
    )
    out["by_source"] = _by_source(r["data_source"] for r in rows)
    keys = ("finding_id", "title", "description", "severity", "data_source")
    out["items"] = [
        {
            **{k: r[k] for k in keys},
            "created_at": r["created_at"],
            "case_id": r["case_id"],
        }
        for r in rows[offset : offset + limit]
    ]
    if state in IN_CASE:
        counts = Counter(r["case_id"] for r in rows if r["case_id"])
        by_id = {c.get("case_id"): c for c in cases}
        out["cases"] = _sort_cases(
            [
                {
                    "case_id": case_id,
                    "title": by_id[case_id].get("title"),
                    "status": by_id[case_id].get("status"),
                    "priority": by_id[case_id].get("priority"),
                    "updated_at": by_id[case_id].get("updated_at"),
                    "alerts": n,
                }
                for case_id, n in counts.items()
                if case_id in by_id
            ]
        )
    return out


@router.get("/outcomes/{state}")
def outcome_alerts(
    state: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    """Today's alerts in one outcome, newest first, and the cases they sit in."""
    if state not in LABELS:
        raise HTTPException(status_code=404, detail=f"Unknown outcome: {state}")
    if _demo_on():
        return _demo_outcome(state, limit, offset)
    ids = [
        r["finding_id"] for r in terminal_states_today() if r["terminal_state"] == state
    ]
    out = _empty(state, len(ids), offset)
    if not ids:
        return out
    db = get_db_manager()
    with db.session_scope() as session:
        rows = (
            session.query(
                Finding.finding_id,
                Finding.title,
                Finding.description,
                Finding.severity,
                Finding.data_source,
                Finding.created_at,
            )
            .filter(Finding.finding_id.in_(ids))
            .order_by(Finding.created_at.desc().nullslast(), Finding.finding_id)
            .all()
        )
        out["by_source"] = _by_source(r.data_source for r in rows)
        page = rows[offset : offset + limit]
        linked = finding_states([r.finding_id for r in page])
        out["items"] = [
            {
                "finding_id": r.finding_id,
                "title": r.title,
                "description": r.description,
                "severity": r.severity,
                "data_source": r.data_source,
                "created_at": _iso(r.created_at),
                "case_id": linked.get(r.finding_id, (None, None))[1],
            }
            for r in page
        ]
        # The cases behind the whole outcome, not just this page.
        if state in IN_CASE:
            all_cases = Counter(
                case_id for _s, case_id in finding_states(ids).values() if case_id
            )
            if all_cases:
                cases = (
                    session.query(
                        Case.case_id,
                        Case.title,
                        Case.status,
                        Case.priority,
                        Case.updated_at,
                    )
                    .filter(Case.case_id.in_(list(all_cases)))
                    .all()
                )
                out["cases"] = _sort_cases(
                    [
                        {
                            "case_id": c.case_id,
                            "title": c.title,
                            "status": c.status,
                            "priority": c.priority,
                            "updated_at": _iso(c.updated_at),
                            "alerts": all_cases[c.case_id],
                        }
                        for c in cases
                    ]
                )
    return out


def _run_row(run: WorkflowRun) -> Dict[str, Any]:
    ctx = run.trigger_context or {}
    finding_ids = ctx.get("finding_ids") or []
    return {
        "run_id": run.run_id,
        "status": run.status,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "duration_ms": run.duration_ms,
        "cost_usd": float(run.total_cost_usd or 0),
        "outcome": run.outcome,
        "summary": run.result_summary or run.reason or run.error,
        "triggered_by": run.triggered_by,
        "case_id": ctx.get("case_id"),
        "finding_id": ctx.get("finding_id")
        or (finding_ids[0] if finding_ids else None),
    }


def _record(window: list) -> Dict[str, Any]:
    tally = Counter(status for status, _d, _c in window)
    # A free or instant run still counts: only a missing value is left out.
    durations = sorted(d for _s, d, _c in window if d is not None)
    costs = [float(c) for _s, _d, c in window if c is not None]
    ended = sum(tally.values())
    return {
        "days": RUN_WINDOW.days,
        "runs": ended,
        "completed": tally.get("completed", 0),
        "failed": tally.get("failed", 0),
        "cancelled": tally.get("cancelled", 0),
        "rate": tally.get("completed", 0) / ended if ended else None,
        "median_ms": durations[len(durations) // 2] if durations else None,
        "mean_cost_usd": sum(costs) / len(costs) if costs else None,
    }


@router.get("/agents/{workflow_id}")
def agent_detail(
    workflow_id: str, service=Depends(provide_workflows)
) -> Dict[str, Any]:
    """One workflow as the board shows it: what it does, live runs, recent runs, 30-day record."""
    definition = catalog.detail(service, workflow_id)
    if definition is None:
        raise HTTPException(
            status_code=404, detail=f"Workflow not found: {workflow_id}"
        )
    if _demo_on():
        # Demo mode does not read stored runs, as the board's agent cards do not.
        return {"workflow": definition, "live": [], "recent": [], "record": _record([])}
    since = utcnow().replace(tzinfo=None) - RUN_WINDOW
    db = get_db_manager()
    with db.session_scope() as session:
        base = session.query(WorkflowRun).filter(
            WorkflowRun.workflow_id == workflow_id, WorkflowRun.deleted_at.is_(None)
        )
        live = (
            base.filter(WorkflowRun.status.in_(IN_FLIGHT_STATUSES))
            .order_by(WorkflowRun.started_at.desc())
            .limit(RECENT_RUNS)
            .all()
        )
        recent = (
            base.filter(WorkflowRun.status.in_(FINISHED))
            .order_by(WorkflowRun.started_at.desc())
            .limit(RECENT_RUNS)
            .all()
        )
        # The record counts hidden runs too, as the board's finish rate does.
        window = (
            session.query(WorkflowRun)
            .filter(
                WorkflowRun.workflow_id == workflow_id,
                WorkflowRun.started_at >= since,
                WorkflowRun.status.in_(FINISHED),
            )
            .with_entities(
                WorkflowRun.status, WorkflowRun.duration_ms, WorkflowRun.total_cost_usd
            )
            .all()
        )
        live_rows = [_run_row(r) for r in live]
        recent_rows = [_run_row(r) for r in recent]
    return {
        "workflow": definition,
        "live": live_rows,
        "recent": recent_rows,
        "record": _record(window),
    }
