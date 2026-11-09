"""Audit retention and erasure (#2769) — console wiring.

The trail reads (entries, export, poll, verify) are the frozen contract in
``core/api/v1/audit_router.py``, served at ``/api/v1/audit`` and here at
``/api/audit``. Retention reads need ``audit.read``; setting the period and
erasing a principal need ``audit.manage`` as a person.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.audit import retention
from core.auth.permissions import require_permission, require_person_permission
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import User

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/audit",
    tags=["audit"],
    auth=Auth.REQUIRED,
)

_READ = require_permission("audit.read")
_MANAGE = require_person_permission("audit.manage")


class PeriodIn(BaseModel):
    days: int = 0


class EraseIn(BaseModel):
    principal_id: str = ""
    reason: str = ""


def _actor(user: User) -> str:
    if not user.principal_id:
        raise HTTPException(status_code=403, detail="A principal is required")
    return user.principal_id


def _retention(fn):
    try:
        return fn()
    except retention.RetentionError as exc:
        status = 404 if str(exc) == "unknown principal" else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.get("/retention")
def get_retention(session: UnitOfWorkSession, _: User = Depends(_READ)):
    """The period in force, the one waiting, and which announcements have not left the box."""
    return retention.view(session, datetime.now(timezone.utc))


@router.put("/retention")
def put_retention(
    body: PeriodIn,
    session: UnitOfWorkSession,
    user: User = Depends(_MANAGE),
):
    """Set the retention period. At least 365 and at most 36500 days, in force seven days later."""
    return _retention(
        lambda: retention.set_period(
            session,
            body.days,
            _actor(user),
            datetime.now(timezone.utc),
            updated_by=user.user_id,
        )
    )


@router.post("/erasure")
def erase_principal(
    body: EraseIn,
    session: UnitOfWorkSession,
    user: User = Depends(_MANAGE),
):
    """Blank one principal's label and trail details, deactivate their account, and end their sign-in and credentials.

    An audit manager waits seven days and needs a reason.
    """
    return _retention(
        lambda: retention.erase(
            session,
            _actor(user),
            body.principal_id,
            body.reason,
            datetime.now(timezone.utc),
            updated_by=user.user_id,
        )
    )
