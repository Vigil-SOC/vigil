"""What a click on the Overview board opens.

The board counts today's alerts by outcome and lists the workflows
that run. These two reads are what a click on one of them opens: the alerts
behind an outcome count (and the cases they sit in), and one workflow's steps,
live runs and recent runs.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import Any, Dict, Optional

from core.findings.alert_outcomes import finding_states, terminal_states_today
from core.findings.overview import _NODE_LABELS
from core.storage.connection import get_db_manager
from core.storage.models import Case, Finding, WorkflowRun
from core.time import utcnow
from core.workflows import catalog
from core.workflows.workflows_service import WorkflowsService

# The board's node labels, so a drill-down is titled as the node it opened from.
LABELS = _NODE_LABELS
EXPLAIN = {
    "resolved_auto": "Today's alerts whose case an agent closed without a person.",
    "resolved_person": "Today's alerts whose case an analyst closed.",
    "working": "Today's alerts in a case that is still open and has nothing waiting on a person.",
    "needs_you": "Today's alerts in a case with an approval waiting on a person.",
    "dismissed": "Today's alerts triage dropped as noise, or someone marked as noise.",
    "scoring": "Today's alerts queued for triage that have not been scored yet.",
    "waiting": "Today's alerts that are not in a case, not queued for triage and not dropped.",
}
RUN_WINDOW = timedelta(days=30)
RECENT_RUNS = 12


def _iso(t: Any) -> Optional[str]:
    return t.isoformat() if hasattr(t, "isoformat") else None


def outcome_alerts(
    state: str, limit: int = 50, offset: int = 0
) -> Optional[Dict[str, Any]]:
    """Today's alerts in one outcome, newest first, and the cases they sit in.

    ``None`` for a state the board does not count.
    """
    if state not in LABELS:
        return None
    ids = [
        r["finding_id"] for r in terminal_states_today() if r["terminal_state"] == state
    ]
    out: Dict[str, Any] = {
        "state": state,
        "label": LABELS[state],
        "explain": EXPLAIN[state],
        "total": len(ids),
        "offset": offset,
        "items": [],
        "cases": [],
        "by_source": [],
    }
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
        out["by_source"] = [
            {"data_source": s, "count": n}
            for s, n in Counter(r.data_source for r in rows).most_common()
        ]
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
        if state in ("resolved_auto", "resolved_person", "working", "needs_you"):
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
                out["cases"] = sorted(
                    (
                        {
                            "case_id": c.case_id,
                            "title": c.title,
                            "status": c.status,
                            "priority": c.priority,
                            "updated_at": _iso(c.updated_at),
                            "alerts": all_cases[c.case_id],
                        }
                        for c in cases
                    ),
                    key=lambda c: (-c["alerts"], c["updated_at"] or ""),
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


def agent_detail(
    workflow_id: str, service: WorkflowsService
) -> Optional[Dict[str, Any]]:
    """One workflow as the board shows it: what it does, live runs, recent runs, 30-day record.

    ``None`` when there is no such workflow.
    """
    definition = catalog.detail(service, workflow_id)
    if definition is None:
        return None
    since = utcnow().replace(tzinfo=None) - RUN_WINDOW
    db = get_db_manager()
    with db.session_scope() as session:
        base = session.query(WorkflowRun).filter(
            WorkflowRun.workflow_id == workflow_id, WorkflowRun.deleted_at.is_(None)
        )
        live = (
            base.filter(WorkflowRun.status.in_(("running", "paused")))
            .order_by(WorkflowRun.started_at.desc())
            .limit(RECENT_RUNS)
            .all()
        )
        recent = (
            base.filter(WorkflowRun.status.in_(("completed", "failed", "cancelled")))
            .order_by(WorkflowRun.started_at.desc())
            .limit(RECENT_RUNS)
            .all()
        )
        window = (
            base.filter(
                WorkflowRun.started_at >= since,
                WorkflowRun.status.in_(("completed", "failed", "cancelled")),
            )
            .with_entities(
                WorkflowRun.status, WorkflowRun.duration_ms, WorkflowRun.total_cost_usd
            )
            .all()
        )
        live_rows = [_run_row(r) for r in live]
        recent_rows = [_run_row(r) for r in recent]
    tally = Counter(status for status, _d, _c in window)
    durations = sorted(d for _s, d, _c in window if d)
    costs = [float(c) for _s, _d, c in window if c]
    ended = sum(tally.values())
    return {
        "workflow": definition,
        "live": live_rows,
        "recent": recent_rows,
        "record": {
            "days": RUN_WINDOW.days,
            "runs": ended,
            "completed": tally.get("completed", 0),
            "failed": tally.get("failed", 0),
            "cancelled": tally.get("cancelled", 0),
            "rate": tally.get("completed", 0) / ended if ended else None,
            "median_ms": durations[len(durations) // 2] if durations else None,
            "mean_cost_usd": sum(costs) / len(costs) if costs else None,
        },
    }
