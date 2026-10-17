"""Monitor reads for the console: the Fleet Wall. Polled.

The response models in ``core/monitor/models.py`` are the contract: every
answer is validated against them, and the console's types are generated from
them. In demo mode the routes serve ``core/monitor/fixtures.py``. Otherwise
they call the reads in ``core/monitor``, which are not implemented yet; until
they are, each route answers 501 and the screen shows its error state.
"""

from typing import Callable, TypeVar

from fastapi import APIRouter, HTTPException, Query

from core.config import is_demo_mode
from core.monitor import fixtures
from core.monitor.common import NotImplementedYet
from core.monitor.models import (
    FleetWallPayload,
)
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
