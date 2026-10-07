"""One reading of a case's state, shared by the case page and later the list.

A case status and an investigation status are different facts. The pill is
this function, not a copy of it in the client.
"""

from __future__ import annotations

from datetime import timezone
from typing import Optional, Sequence

from core.agents.projections import run_id_for
from core.cases.combined_state import budget_health as _budget_health
from core.cases.combined_state import combined_state as _combined_state
from core.storage.models import (
    LIVE_INVESTIGATION_STATUSES,
    CaseClosureInfo,
    Investigation,
    WorkflowRun,
)

# ``workflow_runs`` statuses, in their own words. A run is in flight while
# ``running`` (or ``paused`` on an approval); the terminal words are
# completed / failed / cancelled. These are not investigation statuses, so
# ``LIVE_INVESTIGATION_STATUSES`` never matches them — a hunt that only has a
# run row would otherwise read as not live.
LIVE_WORKFLOW_RUN_STATUSES = ("running", "paused")


def _ref_sort_key(pair: tuple) -> tuple:
    """Newest first, dateless last; naive timestamps read as UTC."""
    when = pair[0]
    if when is None:
        return (1, 0.0)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (0, -when.timestamp())


def combined_state(
    case_status: Optional[str], investigation_statuses: Sequence[str]
) -> str:
    """Newest-first investigation / run statuses, via the queue's one function.

    A closed case is ``closed``. The newest live investigation contributes its
    status, and a live workflow run contributes its own word (``running`` /
    ``paused``) so an in-flight hunt does not leave the pill on ``open``.
    Otherwise the case status stands.
    """
    live = next(
        (
            status
            for status in investigation_statuses
            if status in LIVE_INVESTIGATION_STATUSES
            or status in LIVE_WORKFLOW_RUN_STATUSES
        ),
        None,
    )
    if live is not None and live not in LIVE_INVESTIGATION_STATUSES:
        if (case_status or "").strip() == "closed":
            return "closed"
        return live
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


def workflow_run_ref(run: WorkflowRun) -> dict:
    """The case page's view of one execute-path run.

    Such a run has no ``Investigation`` row, so ``investigation_id`` is
    ``None``; the run is identified by its own ``run_id``. It carries no
    cost cap or iteration count of its own here, so both read as 0.
    """
    started = run.started_at
    cost = float(run.total_cost_usd or 0)
    return {
        "investigation_id": None,
        "status": run.status,
        "workflow_id": run.workflow_id,
        "run_id": run.run_id,
        "live": run.status in LIVE_WORKFLOW_RUN_STATUSES,
        "cost_usd": cost,
        "max_cost_usd": 0,
        "budget_health": budget_health(cost, 0),
        "iteration_count": 0,
        "created_at": started.isoformat() if started is not None else None,
    }


def _merged_refs(
    investigations: Sequence[Investigation],
    workflow_runs: Sequence[WorkflowRun],
) -> list[dict]:
    """Investigation and run refs together, newest first, one per run.

    De-duplicated by ``run_id``: an ``Investigation`` ref's ``run_id`` is
    ``run_id_for`` of its row, which may equal a ``workflow_runs.run_id`` —
    the investigation ref wins there, as it carries the cap and iterations.
    """
    dated: list[tuple] = []
    seen: set[str] = set()
    for item in investigations:
        ref = investigation_ref(item)
        if ref["run_id"] in seen:
            continue
        seen.add(ref["run_id"])
        dated.append((item.created_at, ref))
    for run in workflow_runs:
        ref = workflow_run_ref(run)
        if ref["run_id"] in seen:
            continue
        seen.add(ref["run_id"])
        dated.append((run.started_at, ref))
    # Newest first; a dateless ref sorts last. The sort is stable, so rows
    # sharing a timestamp keep investigation-before-run order.
    dated.sort(key=_ref_sort_key)
    return [ref for _, ref in dated]


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
        "verdict": _verdict(closure),
    }


def detail_fields(
    case_status: Optional[str],
    investigations: Sequence[Investigation],
    closure: Optional[CaseClosureInfo],
    workflow_runs: Sequence[WorkflowRun] = (),
) -> dict:
    """Extras on ``GET /cases/{id}``.

    ``investigations`` and ``workflow_runs`` are each newest first; the
    merged ``investigations`` list interleaves them newest first, so a case
    whose only run came through the execute path (a ``/hunt``) still shows
    that run instead of "No run on this case yet".
    """
    refs = _merged_refs(investigations, workflow_runs)
    return {
        "combined_state": combined_state(case_status, [ref["status"] for ref in refs]),
        "investigations": refs,
        "closure": closure_view(closure),
    }
