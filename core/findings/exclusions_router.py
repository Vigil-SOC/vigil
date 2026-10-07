"""Analyst IP exclusions (``/api/exclusions``).

Console-only, deliberately outside ``/api/v1``: the shape will move while the
feature settles (ranges, expiry and per-source scope are all plausible next
asks), and freezing it now is the regret the v1 README warns about.
Exclusions are org-wide. Listing needs ``findings.read`` like the queue it
filters; adding or removing one needs ``findings.write`` because it changes what
every analyst's queue shows.
"""

from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from core.auth.auth_service import AuthService
from core.auth.current_user import get_current_user
from core.findings.exclusions import (
    ExclusionConflict,
    ExclusionError,
    create_exclusion,
    list_exclusions_with_total,
    remove_exclusion,
)
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import User

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/exclusions",
    tags=["exclusions"],
    auth=Auth.REQUIRED,
)


class ExclusionOut(BaseModel):
    exclusion_id: str
    ip: str
    reason: str
    origin: str
    origin_ref: Optional[str] = None
    created_by: str
    created_at: Optional[str] = None
    removed_at: Optional[str] = None
    removed_by: Optional[str] = None
    removal_reason: Optional[str] = None
    active: bool
    hidden_findings: Optional[int] = Field(
        None, description="Stored findings naming this address (active rows only)."
    )


class ExclusionListResponse(BaseModel):
    exclusions: List[ExclusionOut]
    total: int
    hidden_findings_total: int = Field(
        ..., description="Findings the queue hides, each counted once."
    )


class ExclusionCreate(BaseModel):
    ip: str = Field(..., max_length=64, description="One IPv4 or IPv6 address.")
    reason: str = Field(..., max_length=2000)
    origin: Literal["ad_hoc", "finding", "case", "run"] = "ad_hoc"
    origin_ref: Optional[str] = Field(
        None, max_length=100, description="The finding, case or run it was made from."
    )


class ExclusionRemove(BaseModel):
    reason: Optional[str] = Field(None, max_length=2000)


def _require(user: User, permission: str) -> None:
    if not AuthService.check_permission(user.user_id, permission):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied: {permission} required",
        )


def _actor(user: User) -> str:
    return user.username or user.user_id


@router.get("", response_model=ExclusionListResponse)
def get_exclusions(
    session: UnitOfWorkSession,
    include_removed: bool = False,
    current_user: User = Depends(get_current_user),
):
    """Active exclusions, newest first; ``include_removed`` adds the history."""
    _require(current_user, "findings.read")
    rows, hidden_total = list_exclusions_with_total(
        session, include_removed=include_removed
    )
    return {
        "exclusions": rows,
        "total": len(rows),
        "hidden_findings_total": hidden_total,
    }


@router.post("", response_model=ExclusionOut, status_code=status.HTTP_201_CREATED)
def add_exclusion(
    body: ExclusionCreate,
    session: UnitOfWorkSession,
    current_user: User = Depends(get_current_user),
):
    """Exclude one address. 409 when it is already actively excluded."""
    _require(current_user, "findings.write")
    try:
        return create_exclusion(
            session,
            ip=body.ip,
            reason=body.reason,
            origin=body.origin,
            origin_ref=body.origin_ref,
            created_by=_actor(current_user),
        )
    except ExclusionConflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ExclusionError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/{exclusion_id}/remove", response_model=ExclusionOut)
def remove(
    exclusion_id: str,
    session: UnitOfWorkSession,
    body: Optional[ExclusionRemove] = None,
    current_user: User = Depends(get_current_user),
):
    """Stop excluding the address. The row is kept, marked removed, so the
    queue's history stays explainable; its findings reappear unchanged."""
    _require(current_user, "findings.write")
    try:
        row = remove_exclusion(
            session,
            exclusion_id,
            removed_by=_actor(current_user),
            reason=body.reason if body else None,
        )
    except ExclusionError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if row is None:
        raise HTTPException(status_code=404, detail="Exclusion not found")
    return row
