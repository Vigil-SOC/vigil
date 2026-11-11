"""The session policy document: how long a sign-in lasts, and how many an account holds (#4186).

One row, versioned. GET returns it with its version as the ETag; PUT needs that
version in ``If-Match`` and refuses a stale one with 412. A machine caller (a
service account) may only tighten the policy, and may not change the dormancy period
or the maximum password age.
"""

from __future__ import annotations

from typing import Optional, Union

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert

from core.auth import mfa_coverage, security_keys, session_policy, trail_events
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
MACHINE_DORMANCY = "Changing the dormancy period needs a person's own sign-in"
MACHINE_MAX_AGE = "Changing the maximum password age needs a person's own sign-in"
MACHINE_KEYS = "Requiring security keys needs a person's own sign-in"
NEEDS_OWN_KEY = "Add a security key of your own before requiring them of others"


class Range(BaseModel):
    min: int
    max: int


def _ranges() -> dict[str, Range]:
    """Each bounded number's limits, read off the model's own Field metadata."""
    found = {}
    for name, field in session_policy.SessionPolicy.model_fields.items():
        bounds = {type(m).__name__: m for m in field.metadata}
        if "Ge" in bounds and "Le" in bounds:
            found[name] = Range(min=bounds["Ge"].ge, max=bounds["Le"].le)
    return found


class PolicyOut(session_policy.SessionPolicy):
    # What the model allows, so the console copies no numbers.
    ranges: dict[str, Range]
    version: int
    updated_by: Optional[str] = None
    # Who updated_by is, for a screen that says who changed it.
    updated_by_name: Optional[str] = None
    updated_at: OptDateTime = None
    # Set only on the save that raised Require phishing-resistant MFA.
    without_key: Optional[int] = None
    notice: Optional[str] = None


def _no_key_text(count: int) -> str:
    who = "person has" if count == 1 else "people have"
    return f"{count} {who} no security key; they can't finish signing in until they add one"


class LooseningOut(BaseModel):
    """The answer to a dry run: whether saving this body would loosen the policy."""

    loosening: bool
    # Set only when Require MFA is lowered: active people who could then sign in
    # without MFA, because the new level does not require it of them.
    without_mfa: Optional[int] = None
    # Set only when security keys are required of more people: covered, active
    # people with no key, who can't finish signing in until they add one.
    without_key: Optional[int] = None


class StaleOut(BaseModel):
    detail: str
    current: PolicyOut


def _out(stored: session_policy.Stored, session) -> PolicyOut:
    editor = stored.updated_by and session.get(User, stored.updated_by)
    return PolicyOut(
        **stored.policy.model_dump(),
        ranges=_ranges(),
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
    raising = session_policy.KEY_LEVELS.index(
        body.require_phishing_resistant
    ) > session_policy.KEY_LEVELS.index(old.policy.require_phishing_resistant)
    if dry_run:
        lowered = session_policy.MFA_LEVELS.index(
            body.require_mfa
        ) < session_policy.MFA_LEVELS.index(old.policy.require_mfa)
        # People without MFA the new level does not require it of.
        count = (
            mfa_coverage.without_mfa(session)
            - mfa_coverage.without_mfa(session, body.require_mfa)
            if lowered
            else None
        )
        return LooseningOut(
            loosening=loosening,
            without_mfa=count,
            without_key=(
                mfa_coverage.without_key(session, body.require_phishing_resistant)
                if raising
                else None
            ),
        )
    actor = request.state.vigil_http_actor
    changes = {"old": old.policy.model_dump(), "new": body.model_dump()}
    # Any change to dormancy or the maximum password age, tightening too, is a
    # person's act.
    dormancy = old.policy.dormancy_days != body.dormancy_days
    max_age = old.policy.password_max_age_days != body.password_max_age_days
    if (loosening or dormancy or max_age) and not actor.person:
        reason = (
            MACHINE_LOOSENING
            if loosening
            else MACHINE_DORMANCY if dormancy else MACHINE_MAX_AGE
        )
        trail_events.refused(
            "session_policy.refused",
            actor=user.principal_id,
            reason=reason,
            loosening=loosening,
            **changes,
        )
        raise HTTPException(status_code=403, detail=reason)

    without_key = None
    if raising:
        # Raising tightens, so is_loosening lets a machine do it: it may not. The
        # person needs a key of their own, and keys must be available at all.
        keys = security_keys.availability()
        refusal = None
        if not actor.person:
            refusal = (403, MACHINE_KEYS)
        elif not keys.available:
            refusal = (409, keys.reason)
        elif not security_keys.has_keys(session, user.principal_id):
            refusal = (403, NEEDS_OWN_KEY)
        if refusal:
            trail_events.refused(
                "session_policy.refused",
                actor=user.principal_id,
                reason=refusal[1],
                loosening=loosening,
                **changes,
            )
            raise HTTPException(status_code=refusal[0], detail=refusal[1])
        without_key = mfa_coverage.without_key(session, body.require_phishing_resistant)
        changes["without_key"] = without_key

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
    out = _out(stored, session)
    if without_key is not None:
        out.without_key, out.notice = without_key, _no_key_text(without_key)
    return out
