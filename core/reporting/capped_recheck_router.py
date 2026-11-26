"""Start, resume, and read a capped re-check, and set its run ceiling.

``platform.write`` starts a batch, resumes one, and changes the ceiling.
``platform.read`` reads them. The Day 1 Report renders the projection.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth.permissions import require_permission
from core.reporting.capped_recheck import (
    Refused,
    ceiling_runs,
    latest_projection,
    resume_batch,
    set_ceiling,
    start_batch,
)
from core.routing import Auth, RouterMeta
from core.storage.models import User

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/day1-report/capped-recheck",
    tags=["day1-report"],
    auth=Auth.REQUIRED,
)


class CappedRecheckCeiling(BaseModel):
    runs: int = Field(ge=0)


class GroupCounts(BaseModel):
    top: int
    control: int


class WorstCase(BaseModel):
    calls: int
    dollars: float
    wall_ms: int


class Reserve(BaseModel):
    remainder: Optional[float] = None
    note: str


class Projection(BaseModel):
    batch_id: str
    status: str
    tickets_chosen: int
    could_not_recheck: int
    ceiling_reaches: GroupCounts
    run_count: int
    worst_case: WorstCase
    post_install_closures: int
    control_count: int
    control_short: bool
    reserve: Optional[Reserve] = None


@router.get("/ceiling", response_model=CappedRecheckCeiling)
def read_ceiling(
    _user: User = Depends(require_permission("platform.read")),
) -> CappedRecheckCeiling:
    """The run ceiling. No row is 0, and 0 refuses a start."""
    return CappedRecheckCeiling(runs=ceiling_runs())


@router.put("/ceiling", response_model=CappedRecheckCeiling)
def write_ceiling(
    body: CappedRecheckCeiling,
    user: User = Depends(require_permission("platform.write")),
) -> CappedRecheckCeiling:
    """Set how many re-decision runs one batch may start."""
    if not set_ceiling(body.runs, str(user.user_id)):
        raise HTTPException(status_code=500, detail="Could not save the ceiling.")
    return body


@router.get("", response_model=Projection)
def read_batch(
    _user: User = Depends(require_permission("platform.read")),
) -> Projection:
    """The latest capped batch."""
    body = latest_projection()
    if body is None:
        raise HTTPException(status_code=404, detail="No capped re-check yet.")
    return Projection.model_validate(body)


@router.post("", response_model=Projection)
async def start(
    user: User = Depends(require_permission("platform.write")),
) -> Projection:
    """Draw the next capped batch and start runs up to the two in flight."""
    try:
        body = await start_batch(user.principal_id)
    except Refused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return Projection.model_validate(body)


@router.post("/resume", response_model=Projection)
async def resume(
    _user: User = Depends(require_permission("platform.write")),
) -> Projection:
    """Continue the open batch. The sweep does not do this."""
    try:
        body = await resume_batch()
    except Refused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return Projection.model_validate(body)
