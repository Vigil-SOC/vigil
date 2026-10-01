"""One reading of a case's state, shared by the case page and later the list.

A case status and an investigation status are different facts. The pill is
this function, not a copy of it in the client.
"""

from __future__ import annotations

from typing import Optional, Sequence

from core.agents.projections import run_id_for
from core.storage.models import (
    LIVE_INVESTIGATION_STATUSES,
    CaseClosureInfo,
    Investigation,
)

# The same cuts CaseSLAService uses for SLA health. Budget on the case page
# uses them here so the client does not grow a second copy.
_BUDGET_CRITICAL = 90
_BUDGET_WARNING = 75


def combined_state(
    case_status: Optional[str], investigation_statuses: Sequence[str]
) -> str:
    """Newest-first investigation statuses.

    A closed case is ``closed``. A live investigation contributes its status.
    Otherwise the case status stands, including an open case whose run has
    finished. The run's outcome is not this pill.
    """
    if case_status == "closed":
        return "closed"
    for status in investigation_statuses:
        if status in LIVE_INVESTIGATION_STATUSES:
            return status
    return case_status or "open"


def budget_health(cost_usd: float, max_cost_usd: float) -> str:
    """``healthy`` / ``warning`` / ``critical``, from spend against the cap."""
    if max_cost_usd <= 0:
        return "healthy"
    spent = (cost_usd / max_cost_usd) * 100
    if spent >= _BUDGET_CRITICAL:
        return "critical"
    if spent >= _BUDGET_WARNING:
        return "warning"
    return "healthy"


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
        "verdict": _verdict(closure),
    }


def detail_fields(
    case_status: Optional[str],
    investigations: Sequence[Investigation],
    closure: Optional[CaseClosureInfo],
) -> dict:
    """Extras on ``GET /cases/{id}``. ``investigations`` is newest first."""
    return {
        "combined_state": combined_state(
            case_status, [item.status for item in investigations]
        ),
        "investigations": [investigation_ref(item) for item in investigations],
        "closure": closure_view(closure),
    }
