"""The Monitor contract: what ``GET /api/fleet-wall`` answers.

These models are the routes' ``response_model``, so every implementation of the
read in ``wall.py`` is validated against them, and the
console's types are generated from them (``Schema<'FleetWallPayload'>`` etc. in
``clients/web/src/services/api.ts``). Where a field's meaning is still an open
decision, its description says so; settle it here, not in an implementation.

Every number is a ``MonitorMetric`` (or a subclass adding the extras one gauge
needs). A number Vigil does not record yet is ``value=None`` with
``unmeasured_text`` and ``unmeasured_reason`` set, never a guessed zero.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Level = Literal["good", "fair", "poor"]
WallState = Literal["needs_you", "running", "paused", "waiting", "stuck", "on_hold"]


class Contract(BaseModel):
    """Every field is always sent: a default only fills it in on the server, so
    the console's generated types read ``T | null``, never ``T | undefined``."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class MonitorMetric(Contract):
    """One number as the console shows it: value, limit, level and where it comes from."""

    value: Optional[float] = Field(description="None when unmeasured.")
    limit: Optional[float] = Field(
        default=None, description="The enforced limit, or None when none is set."
    )
    level: Optional[Level] = Field(
        default=None,
        description=(
            "Decided by the server (common.usage_level / rate_level); the console "
            "never derives it. None when there is no limit or no value."
        ),
    )
    info: str = Field(
        description="The ⓘ line: where the number comes from and how it is counted."
    )
    unmeasured_text: Optional[str] = Field(
        default=None, description="'Not measured yet' when value is None."
    )
    unmeasured_reason: Optional[str] = Field(
        default=None, description="Why it is not measured."
    )


# ---------------------------------------------------------------- Fleet Wall


class WallNextAction(Contract):
    count: int = Field(
        description="Decisions waiting on a person (core.response.approval_service.needs_you)."
    )
    case_id: Optional[str] = Field(default=None, description="The oldest one's case.")
    title: Optional[str] = None
    reason: Optional[str] = None


class WallSlots(MonitorMetric):
    """Cases running at once against the concurrency cap.

    Open decision: what "slots" counts. Today: open cases whose run is executing
    or assigned, against ``orchestrator_max_agents``. If fleet limits land
    (Redis model/tool gate, human lane), read those instead or rename the gauge.
    """

    near_limit: int = Field(
        description="Open cases that have spent >= 75% of max_cost_usd."
    )


class WallClosedToday(MonitorMetric):
    """Cases closed since midnight UTC."""

    yesterday: int = Field(
        description="Closed yesterday from midnight up to this time of day."
    )
    without_person: Optional[float] = Field(
        default=None,
        description=(
            "Share (0..1) closed by agents. A closure with closed_by_kind NULL is "
            "not an agent close. None when nothing closed today."
        ),
    )


class WallFacet(Contract):
    key: str = Field(
        description="all, needs_you, stuck, near_limit, running, paused, on_hold, in that order."
    )
    label: str
    count: Optional[int] = Field(description="None when the state is not recorded yet.")
    unmeasured_reason: Optional[str] = None


class WallDecision(Contract):
    title: Optional[str] = None
    reason: Optional[str] = None
    since: Optional[str] = Field(
        default=None, description="ISO time the decision was asked."
    )


class WallCell(Contract):
    """One open case. Built through core.cases.combined_state.queue_item so the
    wall and the Cases page cannot disagree."""

    case_id: str
    title: str
    description: Optional[str] = None
    workflow_id: str
    severity: Optional[str] = None
    state: WallState = Field(
        description=(
            "needs_you: asking a person. running: run executing or assigned. "
            "paused: run paused. stuck: no explanation moved in 6+ steps. "
            "on_hold: held by a person. waiting: anything else."
        )
    )
    reason: str = Field(description="Why it is in this state, in one line.")
    alerts: int = Field(description="Alerts combined into the case.")
    combined: bool = Field(description="More than one alert.")
    heavy: bool = Field(
        description="wall.COMBINED_HEAVY or more alerts; drawn with a thick outline."
    )
    limit_used: Optional[float] = Field(
        default=None,
        description="Share (0..1) of max_cost_usd spent; None when not priced.",
    )
    limit_level: Optional[Level] = None
    near_limit: bool = Field(description="limit_used >= 0.75.")
    steps: Optional[int] = None
    owner: Optional[str] = Field(default=None, description="None means agents own it.")
    sla_seconds_left: Optional[int] = Field(
        default=None, description="Negative once breached; None when no SLA."
    )
    decision: Optional[WallDecision] = Field(
        default=None, description="Set when state is needs_you."
    )
    incident: None = Field(
        default=None, description="Reserved until incident declarations are recorded."
    )


class BandRunning(MonitorMetric):
    """Runs of this workflow running now, against the workflow's slots if it has any."""

    cap_text: Optional[str] = Field(
        default=None, description="How the cap applies, e.g. 'shares the fleet cap'."
    )


class BandRate(MonitorMetric):
    """Share of today's closes for this workflow made without a person."""

    sample: Optional[int] = Field(
        default=None, description="Closes the share is taken over."
    )


class WallBand(Contract):
    """One workflow; unassigned cases last."""

    workflow_id: str
    name: str
    open: int = Field(description="Open cases in this workflow.")
    running: BandRunning
    today: int = Field(description="Runs finished today.")
    cost_per_case: Optional[float] = Field(
        default=None,
        description="Mean USD per case closed today, summing all of a case's runs (not per run).",
    )
    mean_steps: Optional[float] = None
    waiting: int = Field(description="Queued intake rows routed to this workflow.")
    without_person: BandRate
    cells: Optional[list[WallCell]] = Field(
        default=None, description="Every open case in the band, as drawn."
    )


class WallJustClosed(Contract):
    case_id: str
    closed_at: str = Field(description="ISO time.")
    by: Literal["agents", "a person"]


class WallInfo(Contract):
    cell: str = Field(description="The ⓘ text for the squares.")
    kind: str = Field(description="The ⓘ text for the bands.")


class FleetWallPayload(Contract):
    """``GET /api/fleet-wall``. Polled every 10 s; keep it cheap (no unindexed scans)."""

    now: str = Field(description="ISO time the payload was built.")
    next_action: Optional[WallNextAction] = None
    slots: WallSlots
    closed_today: WallClosedToday
    incidents_today: MonitorMetric
    load: MonitorMetric = Field(
        description="Queued intake rows against the intake high-water mark."
    )
    facets: list[WallFacet]
    bands: list[WallBand]
    omitted: int = Field(description="Open cases past wall.WALL_LIMIT, not drawn.")
    just_closed: list[WallJustClosed] = Field(description="Newest first.")
    just_closed_minutes: int
    info: WallInfo
