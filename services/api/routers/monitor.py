"""Monitor reads for the console: the Fleet Wall and Pulse. Polled.

The response models in ``core/monitor/models.py`` are the contract: every
answer is validated against them, and the console's types are generated from
them. In demo mode the routes serve ``core/monitor/fixtures.py``. Otherwise
they call the reads in ``core/monitor``, which are not implemented yet; until
they are, each route answers 501 and the screens show their error state.
"""

from typing import Callable, TypeVar

from fastapi import APIRouter, HTTPException, Query

from core.config import is_demo_mode
from core.monitor import fixtures
from core.monitor.common import NotImplementedYet
from core.monitor.models import (
    FleetWallPayload,
    PulseDecisions,
    PulseSummary,
    PulseThroughput,
    PulseWork,
)
from core.monitor.pulse import decisions, summary, throughput, work
from core.monitor.wall import JUST_CLOSED_MINUTES, wall_payload
from core.routing import Auth, RouterMeta

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api",
    tags=["monitor"],
    auth=Auth.REQUIRED,
)

T = TypeVar("T")


def _read(
    live: Callable[..., T], sample: Callable[..., T], unbuilt: str, **kwargs
) -> T:
    if is_demo_mode():
        return sample(**kwargs)
    try:
        return live(**kwargs)
    except NotImplementedYet:
        raise HTTPException(status_code=501, detail=unbuilt)


@router.get("/fleet-wall", response_model=FleetWallPayload)
def get_fleet_wall(
    just_closed_minutes: int = Query(default=JUST_CLOSED_MINUTES, ge=1, le=240),
):
    """Every open case as a cell, grouped by workflow, with the fleet strip."""
    return _read(
        wall_payload,
        fixtures.wall_payload,
        "The Fleet Wall read is not implemented yet.",
        just_closed_minutes=just_closed_minutes,
    )


_PULSE_UNBUILT = "This Pulse read is not implemented yet."


@router.get("/pulse/summary", response_model=PulseSummary)
def get_pulse_summary():
    """Pulse Summary: the gauges, what needs attention, the day, the adjustments."""
    return _read(summary, fixtures.summary, _PULSE_UNBUILT)


@router.get("/pulse/throughput", response_model=PulseThroughput)
def get_pulse_throughput():
    """Pulse Throughput: the day and work by workflow."""
    return _read(throughput, fixtures.throughput, _PULSE_UNBUILT)


@router.get("/pulse/work", response_model=PulseWork)
def get_pulse_work():
    """Pulse Paused and stuck: what waits, by age."""
    return _read(work, fixtures.work, _PULSE_UNBUILT)


@router.get("/pulse/decisions", response_model=PulseDecisions)
def get_pulse_decisions():
    """Pulse Decisions: who decided, and why agents stopped to ask."""
    return _read(decisions, fixtures.decisions, _PULSE_UNBUILT)
