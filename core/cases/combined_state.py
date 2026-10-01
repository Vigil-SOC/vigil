"""Combined case state, shared by the queue and ``GET /cases/{id}``.

A closed case is ``closed``. A live investigation contributes its status.
Otherwise the case status stands. SLA and budget health use the same 75 / 90
elapsed cuts as ``CaseSLAService.get_sla_status``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from core.storage.models.workflow import LIVE_INVESTIGATION_STATUSES

# Fractions of the clock, matching ``get_sla_status`` (warning at 75%, critical
# at 90%). The queue's SQL filter uses the warning cut so it does not call
# ``get_sla_status`` once per case.
HEALTH_WARNING = 0.75
HEALTH_CRITICAL = 0.90

CLOSED = "closed"


def combined_state(case_status: str | None, investigation_status: str | None) -> str:
    """State the queue and the case read agree on."""
    if (case_status or "").strip() == CLOSED:
        return CLOSED
    if investigation_status in LIVE_INVESTIGATION_STATUSES:
        return investigation_status
    return (case_status or "").strip()


def health_word(elapsed_ratio: float) -> str:
    """Map an elapsed fraction to healthy / warning / critical.

    ``elapsed_ratio`` is elapsed / budget (0.9 means 90%). A past-due SLA is
    ``breached`` before this runs; crossing the budget is critical, which is
    what 100% of the clock already is.
    """
    percent = elapsed_ratio * 100.0
    if percent >= HEALTH_CRITICAL * 100.0:
        return "critical"
    if percent >= HEALTH_WARNING * 100.0:
        return "warning"
    return "healthy"


def budget_health(cost_usd: float | None, max_cost_usd: float | None) -> str | None:
    """Health of ``cost_usd / max_cost_usd`` on the 75 / 90 cuts."""
    if cost_usd is None or max_cost_usd is None or max_cost_usd <= 0:
        return None
    return health_word(cost_usd / max_cost_usd)


def sla_clock(
    *,
    now: datetime,
    has_sla: bool,
    sla_created_at: datetime | None,
    response_due: datetime | None,
    resolution_due: datetime | None,
    response_completed_at: datetime | None,
    resolution_completed_at: datetime | None,
    is_paused: bool,
) -> tuple[str | None, float | None]:
    """``(health_status, resolution seconds left)`` for one SLA row.

    Seconds left is signed (overdue is negative) while the resolution clock is
    running, and ``None`` when there is no SLA, it is paused, or resolution
    already completed. Health follows ``get_sla_status``: a past-due open
    clock is breached, otherwise the worse of the two elapsed percents.
    """
    if not has_sla or sla_created_at is None:
        return None, None
    if is_paused:
        return "healthy", None

    def percent(due: datetime | None, completed: datetime | None) -> float:
        if completed is not None or due is None:
            return 0.0
        total = (due - sla_created_at).total_seconds()
        if total <= 0:
            return 0.0
        elapsed = (now - sla_created_at).total_seconds()
        return min(100.0, (elapsed / total) * 100.0)

    def past_due(due: datetime | None, completed: datetime | None) -> bool:
        return completed is None and due is not None and now > due

    breached = past_due(response_due, response_completed_at) or past_due(
        resolution_due, resolution_completed_at
    )
    if breached:
        health = "breached"
    else:
        worst = max(
            percent(response_due, response_completed_at),
            percent(resolution_due, resolution_completed_at),
        )
        health = health_word(worst / 100.0)

    seconds_left = None
    if resolution_completed_at is None and resolution_due is not None:
        seconds_left = (resolution_due - now).total_seconds()
    return health, seconds_left


@dataclass(frozen=True)
class QueueItem:
    """List item after combined state and the health words."""

    case_id: str
    title: str
    priority: Optional[str]
    assignee: Optional[str]
    combined_state: str
    workflow_id: Optional[str]
    findings_count: int
    iteration_count: Optional[int]
    cost_usd: Optional[float]
    max_cost_usd: Optional[float]
    budget_health: Optional[str]
    comment_count: int
    last_activity: Optional[datetime]
    age_seconds: float
    sla_seconds_left: Optional[float]
    health_status: Optional[str]


def queue_item(row, now: datetime) -> QueueItem:
    """Apply combined state and the 75 / 90 health words to one SQL row."""
    health, seconds_left = sla_clock(
        now=now,
        has_sla=row.has_sla,
        sla_created_at=row.sla_created_at,
        response_due=row.response_due,
        resolution_due=row.resolution_due,
        response_completed_at=row.response_completed_at,
        resolution_completed_at=row.resolution_completed_at,
        is_paused=row.is_paused,
    )
    return QueueItem(
        case_id=row.case_id,
        title=row.title,
        priority=row.priority,
        assignee=row.assignee,
        combined_state=combined_state(row.status, row.live_status),
        workflow_id=row.workflow_id,
        findings_count=row.findings_count,
        iteration_count=row.iteration_count,
        cost_usd=row.cost_usd,
        max_cost_usd=row.max_cost_usd,
        budget_health=budget_health(row.cost_usd, row.max_cost_usd),
        comment_count=row.comment_count,
        last_activity=row.last_activity,
        age_seconds=row.age_seconds,
        sla_seconds_left=seconds_left,
        health_status=health,
    )
