"""The session policy document: how long a sign-in lasts, and how many an account holds (#4186).

One row, versioned. GET returns it with its version as the ETag; PUT needs that
version in ``If-Match`` and refuses a stale one with 412. A machine caller (a
service account) may only tighten the policy.
"""

from __future__ import annotations

from typing import Optional, Union

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert

from core.auth import session_policy, trail_events
from core.auth.permissions import require_permission
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import SystemConfig, User
from core.storage.schemas.base import OptDateTime
from core.time import utcnow

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/session-policy",
    tags=["session-policy"],
    auth=Auth.REQUIRED,
)

_MANAGE = require_permission("sessions.manage")

MACHINE_LOOSENING = "A machine caller may only tighten the session policy"


class PolicyOut(session_policy.SessionPolicy):
    version: int
    updated_by: Optional[str] = None
    # Who updated_by is, for a screen that says who changed it.
    updated_by_name: Optional[str] = None
    updated_at: OptDateTime = None


class LooseningOut(BaseModel):
    """The answer to a dry run: whether saving this body would loosen the policy."""

    loosening: bool


class StaleOut(BaseModel):
    detail: str
    current: PolicyOut


def _out(stored: session_policy.Stored, session) -> PolicyOut:
    editor = stored.updated_by and session.get(User, stored.updated_by)
    return PolicyOut(
        **stored.policy.model_dump(),
        version=stored.version,
        updated_by=stored.updated_by,
        updated_by_name=(editor.full_name or editor.username) if editor else None,
        updated_at=stored.updated_at,
    )


def _etag(version: int) -> str:
    return f'"{version}"'


def _version_of(if_match: Optional[str]) -> int:
    if if_match is None:
        raise HTTPException(status_code=428, detail="If-Match is required")
    try:
        return int(if_match.removeprefix("W/").strip().strip('"'))
    except ValueError:
        raise HTTPException(
            status_code=400, detail="If-Match must be a version from GET"
        ) from None


@router.get("", response_model=PolicyOut)
def get_policy(
    response: Response, session: UnitOfWorkSession, _: User = Depends(_MANAGE)
):
    """The policy in force and its version (the ETag)."""
    stored, _found = session_policy.read_row(session)
    response.headers["ETag"] = _etag(stored.version)
    return _out(stored, session)


@router.put(
    "",
    response_model=Union[PolicyOut, LooseningOut],
    responses={
        412: {"model": StaleOut, "description": "If-Match is not the current version"},
        428: {"description": "If-Match is missing"},
    },
)
def put_policy(
    body: session_policy.SessionPolicy,
    request: Request,
    response: Response,
    session: UnitOfWorkSession,
    user: User = Depends(_MANAGE),
    if_match: Optional[str] = Header(None),
    dry_run: bool = False,
):
    """Replace the policy. An idle limit above its standard, or a stale version, is refused.

    ``dry_run`` checks the version like a save, then saves nothing and answers whether
    the body would loosen the policy now in force.
    """
    expected = _version_of(if_match)

    def stale():
        current = _out(session_policy.read_row(session)[0], session)
        return JSONResponse(
            status_code=412,
            content=StaleOut(
                detail="The policy changed since you read it", current=current
            ).model_dump(mode="json"),
            headers={"ETag": _etag(current.version)},
        )

    # The row stays locked until this request ends.
    old, found = session_policy.read_row(session, lock=not dry_run)
    if old.version != expected:
        return stale()

    loosening = session_policy.is_loosening(old.policy, body)
    if dry_run:
        return LooseningOut(loosening=loosening)
    actor = request.state.vigil_http_actor
    changes = {"old": old.policy.model_dump(), "new": body.model_dump()}
    if loosening and not actor.person:
        trail_events.refused(
            "session_policy.refused",
            actor=user.principal_id,
            reason=MACHINE_LOOSENING,
            loosening=True,
            **changes,
        )
        raise HTTPException(status_code=403, detail=MACHINE_LOOSENING)

    value = {**body.model_dump(), "version": old.version + 1}
    if found:
        row = session.get(SystemConfig, session_policy.KEY)
        row.value, row.updated_by, row.updated_at = value, user.user_id, utcnow()
    else:
        # A concurrent first save holds the same key: this one loses, with 412.
        inserted = session.execute(
            insert(SystemConfig)
            .values(
                key=session_policy.KEY,
                value=value,
                description="Session policy: sign-in limits",
                config_type="auth",
                updated_by=user.user_id,
            )
            .on_conflict_do_nothing()
        )
        if inserted.rowcount == 0:
            return stale()
    trail_events.record(
        session,
        "session_policy.changed",
        actor=user.principal_id,
        loosening=loosening,
        version=old.version + 1,
        **changes,
    )
    session.flush()
    stored = session_policy.read_row(session)[0]
    response.headers["ETag"] = _etag(stored.version)
    return _out(stored, session)
