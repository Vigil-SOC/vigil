"""Read, export, poll and verify the audit trail (#2765) — the frozen part (#4018).

One filter, from ``core.audit.trail``. List, export, and range verify need
``audit.read``. Poll needs a sign-in: a service account passes on
``audit.read``, and anyone else needs ``audit.forward``. A missing sign-in is
refused before that check. A poll writes no entry; a page that carries an
unconfirmed restore entry clears it. Retention and erasure stay console wiring
in ``services/api/routers/audit.py``.

The envelope of an entry is frozen; its payload is open per action, so keys
beyond the named ones pass through untouched.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from core.audit import reads, retention, trail
from core.auth import current_user as current_user_module
from core.auth.auth_service import _is_service_account
from core.auth.current_user import get_current_user
from core.auth.permissions import deny_unless, require_permission
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import User

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/v1/audit",
    tags=["audit"],
    auth=Auth.REQUIRED,
    legacy_prefixes=("/api/audit",),
)

_READ = require_permission("audit.read")
_SIGN_IN_REQUIRED = "A person sign-in is required"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AuditTip(BaseModel):
    """The newest entry, or the anchor when the trail is empty."""

    seq: int
    hash: str


class AuditPrincipal(BaseModel):
    """A principal an entry names, read from principals when the entry is read."""

    id: str
    label: Optional[str] = None
    kind: Optional[str] = None
    deleted: bool
    erased: bool


class AuditPayload(BaseModel):
    """The hashed record. Open per action: other keys pass through."""

    model_config = ConfigDict(extra="allow")

    action: Optional[str] = None
    # A principal or record id, or an object carrying ``id``.
    object: Optional[Any] = None
    outcome: Optional[str] = None
    run_id: Optional[str] = None


class AuditEntry(BaseModel):
    """One trail entry. Columns beside the envelope pass through."""

    model_config = ConfigDict(extra="allow")

    seq: int
    occurred_at: str
    late: bool
    entry_hash: str
    actor: Optional[AuditPrincipal] = None
    payload: AuditPayload


# The same keys as ``core.audit.reads.FEED_FIELDS``.
class AuditFeedHealth(BaseModel):
    """The feed as the Audit section sees it: no host, no certificates."""

    enabled: Optional[bool] = None
    behind: Optional[int] = None
    last_delivered_at: Optional[str] = None
    last_delivered_seq: Optional[int] = None
    last_error: Optional[str] = None
    unconfirmed: Optional[int] = None
    paused: Optional[bool] = None


class AuditPage(BaseModel):
    entries: List[AuditEntry]
    tip: AuditTip
    feed: AuditFeedHealth


class AuditPollPage(BaseModel):
    """Entries after the cursor, or ``reset`` with the latest restore entry
    when the cursor is past the tip. Only the keys of the case in hand are sent."""

    entries: Optional[List[AuditEntry]] = None
    tip: AuditTip
    reset: Optional[bool] = None
    restore: Optional[AuditEntry] = None


class AuditVerification(BaseModel):
    first: Optional[int] = None
    last: Optional[int] = None
    # The first sequence that is missing or does not verify; None when all do.
    first_bad: Optional[int] = None
    reason: str
    ok: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    sign_in: Optional[str] = None,
) -> trail.TrailQuery:
    try:
        return reads.query(
            since=since,
            until=until,
            actor=actor,
            person=person,
            kind=kind,
            area=area,
            object_=object_,
            outcome=outcome,
            attribution=attribution,
            after=after,
            sign_in=sign_in,
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

    A service account's API token stands in for the sign-in; a person's token does not.

    ``permission`` stays ``audit.forward``: the person half of the role matrix
    still denies a Manager, who holds ``audit.read`` and not ``audit.forward``.
    """

    def _check(request: Request, user: User = Depends(get_current_user)) -> User:
        if not (
            current_user_module.is_token_request(request) and _is_service_account(user)
        ):
            _require_sign_in(request)
        permission = "audit.read" if _is_service_account(user) else "audit.forward"
        deny_unless(user.user_id, permission)
        return user

    # The role x route matrix reads these off the dependency (the person audience).
    _check.permission, _check.unsafe_only = "audit.forward", False
    return _check


def _carried(session, rows: list[dict]) -> None:
    retention.carried(session, [row["seq"] for row in rows if "seq" in row])


def _ndjson(rows: list[dict], tip: trail.Tip):
    for row in rows:
        yield json.dumps(row, separators=(",", ":")) + "\n"
    yield json.dumps({"checkpoint": tip.public()}, separators=(",", ":")) + "\n"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


# exclude_unset on both pages: a payload key the entry lacks is not sent as null.
@router.get("/entries", response_model=AuditPage, response_model_exclude_unset=True)
def list_entries(
    session: UnitOfWorkSession,
    query: trail.TrailQuery = Depends(_filters),
    limit: int = Query(100, ge=1, le=trail.POLL_LIMIT),
    _: User = Depends(_READ),
):
    """The trail, oldest sequence first, under the shared filter."""
    return reads.page(session, query, limit)


@router.get(
    "/export",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "One entry per line, then a closing "
            '`{"checkpoint": {"seq", "hash"}}` line.',
            "content": {"application/x-ndjson": {}},
        }
    },
)
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


# A poll page sends entries, or reset and restore; never nulls for the other.
@router.get("/poll", response_model=AuditPollPage, response_model_exclude_unset=True)
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


@router.get("/verify", response_model=AuditVerification)
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
