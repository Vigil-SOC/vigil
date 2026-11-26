"""One reading of a case's state, shared by the case page and the list.

A case status and an investigation status are different facts. The pill is
this function, not a copy of it in the client.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Mapping, Optional, Sequence

from sqlalchemy.orm import object_session

from core.agents.projections import run_id_for
from core.cases.case_record import headline
from core.cases.closure import CHANNEL_AGENT, trust_word
from core.cases.combined_state import budget_health as _budget_health
from core.cases.combined_state import combined_state as _combined_state
from core.storage.models import (
    LIVE_CASE_STATES,
    LIVE_INVESTIGATION_STATUSES,
    RUN_LIVE_STATE,
    CaseClosureInfo,
    Investigation,
    Principal,
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


def _is_uuid(text: str) -> bool:
    try:
        uuid.UUID(text)
        return True
    except ValueError:
        return False


def closure_view(closure: Optional[CaseClosureInfo]) -> Optional[dict]:
    if closure is None:
        return None
    # The line names the person from the kept text. The word is derived.
    shown = (getattr(closure, "closed_by_text", None) or "").strip()
    # No username beside the principal: a run's agent, named as the record names it.
    session = object_session(closure)
    if session is not None and closure.closed_by and shown == closure.closed_by:
        principal = session.get(Principal, closure.closed_by)
        shown = (principal.label if principal else None) or shown
        # A run's dismissal stores its run id: name the workflow that closed it.
        if principal is None and closure.close_channel == CHANNEL_AGENT:
            run = session.get(WorkflowRun, closure.closed_by)
            if run is not None:
                shown = run.workflow_name or run.workflow_id
            else:
                investigation = next(
                    (
                        row
                        for row in session.query(Investigation).filter(
                            Investigation.case_id == closure.case_id
                        )
                        if run_id_for(row.investigation_id) == closure.closed_by
                    ),
                    None,
                )
                if investigation is not None:
                    shown = investigation.workflow_id
                elif _is_uuid(closure.closed_by):
                    shown = "agent"  # a run we no longer have; never the raw id
    kind = trust_word(getattr(closure, "close_channel", None))
    verdict = _verdict(closure)
    return {
        "closure_category": closure.closure_category,
        "false_positive_cause": closure.false_positive_cause,
        "closed_by": shown or closure.closed_by or "",
        "closed_by_kind": kind,
        "closed_at": closure.closed_at.isoformat() if closure.closed_at else None,
        # An agent's note is its whole report, shown below; the banner gets its first sentence.
        "verdict": headline(verdict) if kind == "agent" else verdict,
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
        "workflow_name": run.workflow_name,
        "run_id": run.run_id,
        "live": state is not None,
        "cost_usd": cost,
        "max_cost_usd": cap,
        "budget_health": budget_health(cost, cap),
        "iteration_count": int(
            run.iteration_count if run.iteration_count is not None else steps
        ),
        "created_at": run.started_at.isoformat() if run.started_at else None,
        "spawned_by": spawned_by(run.triggered_by),
    }


def spawned_by(triggered_by: Optional[str]) -> Optional[str]:
    """The run whose handoff started this one, from ``handoff:<run>:<case>``."""
    parts = (triggered_by or "").split(":")
    return parts[1] if len(parts) >= 3 and parts[0] == "handoff" and parts[1] else None


# A run's terminal that says its report does not stand, though status may read completed.
_SHORT_OUTCOMES = ("data_starved", "budget_exhausted", "aborted", "abandoned")
# What picked the case's run, by the tier of the run it picked.
PRIMARY_BASIS = (
    "newest grounded",
    "newest, grounding unread",
    "newest, no run on the case stands",
)


def _tier(ref: Mapping) -> int:
    """0 stands, 1 unread, 2 does not stand; checked 2, then 0, then 1.

    A missing fact is unread, never bad: a live run, a row from before the facts
    were written, a plugin that does not cite all land in 1.
    """
    count = ref.get("citation_count")
    if (
        ref.get("outcome") in _SHORT_OUTCOMES
        or ref.get("status") in ("failed", "cancelled")
        or ref.get("capped") is True
        or count == 0
        or ref.get("grounded") is False
    ):
        return 2
    if (
        ref.get("outcome") == "completed"
        and ref.get("capped") is False
        and count is not None
        and count >= 1
    ):
        return 0
    return 1


def primary_choice(refs: Sequence[dict]) -> tuple[Optional[dict], Optional[str]]:
    """The case's run and what picked it, the one place either is decided.

    A hunt's handoff starts a root-cause run on the case it escalates, and that run
    is newer than the hunt; it is the hunt's follow-up, not the case's run. So only
    refs whose starter is not on the case are eligible, or every ref when a chain of
    handoffs leaves none. Among those, the newest of the best tier: a report that
    stands, then one unread, then one that does not stand. Newest first in, as
    ``case_run_refs`` returns them. ``(None, None)`` only when there is no ref.
    """
    listed = {ref["run_id"] for ref in refs}
    eligible = [ref for ref in refs if ref.get("spawned_by") not in listed] or list(
        refs
    )
    if not eligible:
        return None, None
    tier = min(_tier(ref) for ref in eligible)
    picked = next(ref for ref in eligible if _tier(ref) == tier)
    return picked, PRIMARY_BASIS[tier]


def primary_ref(refs: Sequence[dict]) -> Optional[dict]:
    """The case's run, as ``primary_choice`` picks it."""
    return primary_choice(refs)[0]


def run_ids(
    investigations: Sequence[Investigation], runs: Sequence[WorkflowRun]
) -> list[str]:
    """Every run id ``case_run_refs`` lists, for ``list_run_facts``."""
    return [run_id_for(item.investigation_id) for item in investigations] + [
        run.run_id for run in runs
    ]


def case_run_refs(
    investigations: Sequence[Investigation],
    runs: Sequence[WorkflowRun],
    steps: Mapping[str, int] = {},
    facts: Mapping[str, Mapping] = {},
) -> list[tuple[dict, str]]:
    """Investigation and run refs together, newest first, each with its state word.

    An investigation's run is ``run_id_for`` of it, which may also be a
    ``workflow_runs`` row; that run is listed once, as the investigation. ``steps``
    is phases recorded per run id, for a run that reports no iteration. ``facts`` is
    ``list_run_facts`` of the same ids: what each run's report stands on, merged in.
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
    for _, ref, _ in items:
        ref.update(facts.get(ref["run_id"], {}))
    items.sort(key=lambda item: (item[0], item[1]["run_id"]), reverse=True)
    return [(ref, state) for _, ref, state in items]


def detail_fields(
    case_status: Optional[str],
    investigations: Sequence[Investigation],
    closure: Optional[CaseClosureInfo],
    runs: Sequence[WorkflowRun] = (),
    steps: Mapping[str, int] = {},
    facts: Mapping[str, Mapping] = {},
) -> dict:
    """Extras on ``GET /cases/{id}``. Inputs are newest first; so is the output."""
    refs = case_run_refs(investigations, runs, steps, facts)
    primary, basis = primary_choice([ref for ref, _ in refs])
    for ref, _ in refs:
        ref["primary"] = ref is primary
    return {
        "combined_state": combined_state(case_status, [state for _, state in refs]),
        "investigations": [ref for ref, _ in refs],
        "primary_basis": basis,
        "closure": closure_view(closure),
    }
