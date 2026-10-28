"""Read, export, and poll the audit trail (#2765).

One filter, from ``core.audit.trail``. List, export, and range verify need
``audit.read``. Poll needs a sign-in: a service account passes on
``audit.read``, and anyone else needs ``audit.forward``. A missing sign-in is
refused before that check. ``audit.forward`` and ``audit.manage`` stay a
person's own act inside ``check_permission``, including in dev mode. A poll
writes no entry; a page that carries an unconfirmed restore entry clears it.
Retention reads need ``audit.read``; setting the period and erasing a principal
need ``audit.manage`` as a person (#2769).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.audit import feed, retention, trail
from core.auth import current_user as current_user_module
from core.auth import principals
from core.auth.auth_service import _is_service_account
from core.auth.current_user import get_current_user
from core.auth.permissions import (
    deny_unless,
    require_permission,
    require_person_permission,
)
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
_SIGN_IN_REQUIRED = "A person sign-in is required"
_KINDS = frozenset(
    {
        principals.PERSON,
        principals.SERVICE_ACCOUNT,
        principals.AGENT,
        principals.SYSTEM,
    }
)


def _filters(
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    actor: Optional[str] = None,
    person: Optional[str] = None,
    kind: Optional[str] = None,
    area: Optional[str] = None,
    object_: Optional[str] = Query(None, alias="object"),
    outcome: Optional[str] = None,
    attribution: Optional[str] = None,
    after: Optional[int] = Query(None, ge=0),
) -> trail.TrailQuery:
    kind_c = (kind or "").strip()
    if kind_c and kind_c not in _KINDS:
        raise HTTPException(status_code=422, detail=f"unknown principal kind: {kind_c}")
    try:
        return trail.make_query(
            since=since,
            until=until,
            actor=actor,
            person=person,
            kind=kind,
            area=area,
            object_ref=object_,
            outcome=outcome,
            attribution=attribution,
            after=after,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _require_sign_in(request: Request) -> None:
    """A sid on the access token. Dev mode has no token; the kind check remains."""
    if current_user_module.DEV_MODE:
        return
    claims = getattr(request.state, "access_claims", None)
    if not claims or not claims.get("sid"):
        raise HTTPException(status_code=401, detail=_SIGN_IN_REQUIRED)


def require_forward():
    """A sign-in, then ``audit.read`` for a service account and ``audit.forward`` otherwise.

    ``permission`` stays ``audit.forward``: the person half of the role matrix
    still denies a Manager, who holds ``audit.read`` and not ``audit.forward``.
    """

    def _check(request: Request, user: User = Depends(get_current_user)) -> User:
        _require_sign_in(request)
        permission = "audit.read" if _is_service_account(user) else "audit.forward"
        deny_unless(user.user_id, permission)
        return user

    # The role x route matrix reads these off the dependency (the person audience).
    _check.permission, _check.unsafe_only = "audit.forward", False
    return _check


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


def _carried(session, rows: list[dict]) -> None:
    retention.carried(session, [row["seq"] for row in rows if "seq" in row])


def _ndjson(rows: list[dict], tip: trail.Tip):
    for row in rows:
        yield json.dumps(row, separators=(",", ":")) + "\n"
    yield json.dumps({"checkpoint": tip.public()}, separators=(",", ":")) + "\n"


# What the Audit section shows of the feed. Not the host or the certificates,
# which stay on ``audit.forward``.
_FEED_FIELDS = (
    "enabled",
    "behind",
    "last_delivered_at",
    "last_delivered_seq",
    "last_error",
    "unconfirmed",
    "paused",
)


def _feed(session) -> dict:
    try:
        health = feed.health(session)
    except feed.FeedUnavailable:
        health = {"enabled": False, "behind": 0, "paused": False}
    return {key: health.get(key) for key in _FEED_FIELDS}


@router.get("/entries")
def list_entries(
    session: UnitOfWorkSession,
    query: trail.TrailQuery = Depends(_filters),
    limit: int = Query(100, ge=1, le=trail.POLL_LIMIT),
    _: User = Depends(_READ),
):
    """The trail, oldest sequence first, under the shared filter."""
    return {
        "entries": trail.read(session, query, limit),
        "tip": trail.current_tip(session).public(),
        "feed": _feed(session),
    }


@router.get("/export")
def export_entries(
    session: UnitOfWorkSession,
    query: trail.TrailQuery = Depends(_filters),
    user: User = Depends(_READ),
):
    """NDJSON of the filter, closed by a checkpoint line. Writes one export entry."""
    if not user.principal_id:
        raise HTTPException(status_code=403, detail="A principal is required")
    rows, tip = retention.carry_export(session, query, user.principal_id)
    return StreamingResponse(
        _ndjson(rows, tip),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="audit.ndjson"'},
    )


@router.get("/poll")
def poll_entries(
    session: UnitOfWorkSession,
    query: trail.TrailQuery = Depends(_filters),
    limit: int = Query(trail.POLL_LIMIT, ge=1, le=trail.POLL_LIMIT),
    _: User = Depends(require_forward()),
):
    """Entries after ``after``. A cursor past the tip is a reset, not an empty page."""
    if query.after is None:
        raise HTTPException(status_code=422, detail="poll needs after")
    page = trail.poll(session, query, limit)
    if page.reset:
        return {"reset": True, "tip": page.tip.public(), "restore": page.restore}
    rows = list(page.entries)
    _carried(session, rows)
    return {"entries": rows, "tip": page.tip.public()}


@router.get("/verify")
def verify_range(
    session: UnitOfWorkSession,
    first: Optional[int] = Query(None, ge=0),
    last: Optional[int] = Query(None, ge=0),
    _: User = Depends(_READ),
):
    """``trail.verify`` over a sequence range. The first break, when there is one."""
    result = trail.verify(session, first, last)
    return {
        "first": result.first,
        "last": result.last,
        "first_bad": result.first_bad,
        "reason": result.reason,
        "ok": result.ok,
    }


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
    """Blank one principal's label and trail details, and end their sign-in and credentials.

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
