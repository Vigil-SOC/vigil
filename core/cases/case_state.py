"""One reading of a case's state, shared by the case page and the list.

A case status and an investigation status are different facts. The pill is
this function, not a copy of it in the client.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping, Optional, Sequence

from core.agents.projections import run_id_for
from core.cases.combined_state import budget_health as _budget_health
from core.cases.combined_state import combined_state as _combined_state
from core.storage.models import (
    LIVE_CASE_STATES,
    LIVE_INVESTIGATION_STATUSES,
    RUN_LIVE_STATE,
    CaseClosureInfo,
    Investigation,
    WorkflowRun,
)


def combined_state(
    case_status: Optional[str], investigation_statuses: Sequence[str]
) -> str:
    """Newest-first investigation statuses, via the queue's one function.

    A closed case is ``closed``. The newest live investigation contributes its
    status. Otherwise the case status stands.
    """
    live = next(
        (status for status in investigation_statuses if status in LIVE_CASE_STATES),
        None,
    )
    return _combined_state(case_status, live) or "open"


def budget_health(cost_usd: float, max_cost_usd: float) -> str:
    """The queue's 75 / 90 word. No cap reads as healthy."""
    return _budget_health(cost_usd, max_cost_usd) or "healthy"


def investigation_ref(investigation: Investigation) -> dict:
    """The case page's view of one investigation.

    ``run_id`` is ``run_id_for`` of this row. A shadow adjudication is a
    different id and is not this run.
    """
    created = investigation.created_at
    return {
        "investigation_id": investigation.investigation_id,
        "status": investigation.status,
        "workflow_id": investigation.workflow_id,
        "run_id": run_id_for(investigation.investigation_id),
        "live": investigation.status in LIVE_INVESTIGATION_STATUSES,
        "cost_usd": float(investigation.cost_usd or 0),
        "max_cost_usd": float(investigation.max_cost_usd or 0),
        "budget_health": budget_health(
            float(investigation.cost_usd or 0), float(investigation.max_cost_usd or 0)
        ),
        "iteration_count": int(investigation.iteration_count or 0),
        "created_at": created.isoformat() if created is not None else None,
    }


def _verdict(closure: CaseClosureInfo) -> str:
    for candidate in (
        closure.false_positive_reason,
        closure.root_cause,
        closure.executive_summary,
        closure.closure_notes,
    ):
        if candidate and candidate.strip():
            return candidate.strip()
    return ""


def closure_view(closure: Optional[CaseClosureInfo]) -> Optional[dict]:
    if closure is None:
        return None
    return {
        "closure_category": closure.closure_category,
        "closed_by": closure.closed_by,
        "closed_by_kind": closure.closed_by_kind or "agent",
        "closed_at": closure.closed_at.isoformat() if closure.closed_at else None,
        "verdict": _verdict(closure),
    }


def run_cap(run: WorkflowRun) -> float:
    """The run's cost ceiling: what the agent layer reported, else what it was asked.

    The SQL of ``case_repository._case_runs`` reads the same two, in the same order.
    A run that reported nothing and was asked nothing has no cap, which reads 0.
    """
    if run.max_cost_usd is not None:
        return float(run.max_cost_usd)
    asked = (run.trigger_context or {}).get("max_cost_usd")
    if isinstance(asked, bool) or not isinstance(asked, (int, float, str)):
        return 0.0
    try:
        return max(float(asked), 0.0)
    except ValueError:
        return 0.0


def run_ref(run: WorkflowRun, steps: int = 0) -> dict:
    """The case page's view of a run that has no ``Investigation`` row.

    A hunt's iteration is what it last reported; a run that reports none, compose,
    reads its ``steps``, the phases it has recorded.
    """
    state = RUN_LIVE_STATE.get(run.status)
    cost = float(run.total_cost_usd or 0)
    cap = run_cap(run)
    return {
        "investigation_id": None,
        "status": run.status,
        "workflow_id": run.workflow_id,
        "run_id": run.run_id,
        "live": state is not None,
        "cost_usd": cost,
        "max_cost_usd": cap,
        "budget_health": budget_health(cost, cap),
        "iteration_count": int(
            run.iteration_count if run.iteration_count is not None else steps
        ),
        "created_at": run.started_at.isoformat() if run.started_at else None,
    }


def case_run_refs(
    investigations: Sequence[Investigation],
    runs: Sequence[WorkflowRun],
    steps: Mapping[str, int] = {},
) -> list[tuple[dict, str]]:
    """Investigation and run refs together, newest first, each with its state word.

    An investigation's run is ``run_id_for`` of it, which may also be a
    ``workflow_runs`` row; that run is listed once, as the investigation. ``steps``
    is phases recorded per run id, for a run that reports no iteration.
    """
    items: list[tuple[datetime, dict, str]] = [
        (item.created_at or datetime.min, investigation_ref(item), item.status)
        for item in investigations
    ]
    seen = {ref["run_id"] for _, ref, _ in items}
    items += [
        (
            run.started_at or datetime.min,
            run_ref(run, steps.get(run.run_id, 0)),
            RUN_LIVE_STATE.get(run.status, run.status),
        )
        for run in runs
        if run.run_id not in seen
    ]
    items.sort(key=lambda item: (item[0], item[1]["run_id"]), reverse=True)
    return [(ref, state) for _, ref, state in items]


def detail_fields(
    case_status: Optional[str],
    investigations: Sequence[Investigation],
    closure: Optional[CaseClosureInfo],
    runs: Sequence[WorkflowRun] = (),
    steps: Mapping[str, int] = {},
) -> dict:
    """Extras on ``GET /cases/{id}``. Inputs are newest first; so is the output."""
    refs = case_run_refs(investigations, runs, steps)
    return {
        "combined_state": combined_state(case_status, [state for _, state in refs]),
        "investigations": [ref for ref, _ in refs],
        "closure": closure_view(closure),
    }
