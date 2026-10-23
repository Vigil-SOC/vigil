"""Read, export, and poll the audit trail (#2765).

One filter, from ``core.audit.trail``. List, export, and range verify need
``audit.read``. Poll needs ``audit.forward`` on a person sign-in: a missing
sign-in is refused before the role check, and a non-person is refused inside
``check_permission`` even in dev mode. A poll writes nothing.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from core.audit import trail
from core.auth import current_user as current_user_module
from core.auth import principals
from core.auth.auth_service import AuthService
from core.auth.current_user import get_current_user
from core.auth.permissions import require_permission
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import User

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/audit",
    tags=["audit"],
    auth=Auth.REQUIRED,
)

_READ = require_permission("audit.read")
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
    """Person sign-in, then the kind check, then ``audit.forward``."""

    def _check(request: Request, user: User = Depends(get_current_user)) -> User:
        _require_sign_in(request)
        if not AuthService.check_permission(user.user_id, "audit.forward"):
            raise HTTPException(
                status_code=403, detail="Permission denied: audit.forward required"
            )
        return user

    # The role x route matrix reads these off the dependency.
    _check.permission, _check.unsafe_only = "audit.forward", False
    return _check


def _ndjson(rows: list[dict], tip: trail.Tip):
    for row in rows:
        yield json.dumps(row, separators=(",", ":")) + "\n"
    yield json.dumps({"checkpoint": tip.public()}, separators=(",", ":")) + "\n"


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
    rows, tip = trail.export(session, query, user.principal_id)
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
    return {"entries": list(page.entries), "tip": page.tip.public()}


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
