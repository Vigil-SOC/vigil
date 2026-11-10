"""API tokens: a person mints and rotates their own, and a ``users.write`` holder
sees every token and revokes any (#4223).

A token is a machine caller whoever holds it: it cannot mint, rotate or change
an account. It lives outside ``/api/auth`` so that refusal is this module's own.
Storage and the lifetime rule are ``core.auth.mcp_credential_service``.
"""

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.auth import current_user as current_user_module
from core.auth import mcp_credential_service as tokens
from core.auth import step_up, trail_events
from core.auth.auth_service import AuthService, _is_service_account
from core.auth.permissions import STEP_UP, deny_unless, missing_permission
from core.platform.notices import notify
from core.routing import Auth, RouterMeta
from core.storage.models import McpCredential, User
from core.storage.unit_of_work import unit_of_work
from services.api.middleware.auth import get_current_active_user

logger = logging.getLogger(__name__)
router = APIRouter()

ROUTER_META = RouterMeta(prefix="/api/tokens", tags=["tokens"], auth=Auth.REQUIRED)

USERS_WRITE = "users.write"
NOTICE_LINK = "/settings?section=users"


class MintRequest(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    # None is the default for the holder; the rule is tokens.lifetime.
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=tokens.PERSON_MAX_DAYS)


class RotateRequest(BaseModel):
    # The old token ends now instead of in 24 hours.
    suspected_compromise: bool = False
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=tokens.PERSON_MAX_DAYS)


def _holder(user: User) -> dict:
    return {
        "user_id": user.user_id,
        "username": user.username,
        "full_name": user.full_name,
        "service_account": _is_service_account(user),
    }


def _view(credential: McpCredential, holder: User) -> dict:
    return {**credential.to_dict(), "holder": _holder(holder)}


def _tell(holder: User, title: str, message: str, priority: str = "normal") -> None:
    """The holder is told. Never the token: notices are emailed."""
    try:
        notify(
            holder.principal_id,
            "api_token",
            title,
            message,
            link=NOTICE_LINK,
            priority=priority,
        )
    except Exception:  # noqa: BLE001 - the act stands; the notice is not its record
        logger.exception("Could not notify the holder of a token change")


def _refuse_token_request(request: Request, what: str) -> None:
    if current_user_module.is_token_request(request):
        raise HTTPException(status_code=403, detail=f"An API token can't {what}")


def _confirm(request: Request, user: User, action: str) -> None:
    """A token is never asked to confirm it's you, so a new one asks as its holder's
    own privileged writes would: inside the window, for whoever holds such a permission."""
    held = AuthService.get_user_permissions(user.user_id)
    if any(held.get(permission) for permission in STEP_UP):
        step_up.check(request, user.user_id, action)


def _may_mint_for(caller: User, holder: User) -> bool:
    """Today only a person for themselves; a service account's minter is a later route."""
    return caller.user_id == holder.user_id and not _is_service_account(caller)


def _manages(user: User) -> bool:
    return missing_permission(user.user_id, USERS_WRITE) is None


def _found(credential_id: str) -> tuple[McpCredential, User]:
    with unit_of_work() as session:
        credential = tokens.get(credential_id, session)
        holder = session.get(User, credential.user_id) if credential else None
    if credential is None or holder is None:
        raise HTTPException(status_code=404, detail="Token not found")
    return credential, holder


def _date(value) -> str:
    return value.strftime("%Y-%m-%d") if value else "never"


@router.get("")
def list_own(current_user: User = Depends(get_current_active_user)):
    """Your own tokens, newest first, revoked and ended ones included."""
    with unit_of_work() as session:
        rows = tokens.list_all(current_user.user_id, session)
    return {"tokens": [_view(c, u) for c, u in rows]}


@router.get("/all")
def list_all(
    holder: Optional[str] = Query(None, description="A holder's user id"),
    status: Optional[Literal["active", "rotated", "expired", "revoked"]] = None,
    current_user: User = Depends(get_current_active_user),
):
    """Every token, for a ``users.write`` holder; filtered on the server.

    ``holders`` is everyone who holds one, whatever the filter, for its menu."""
    deny_unless(current_user.user_id, USERS_WRITE)
    with unit_of_work() as session:
        rows = tokens.list_all(holder, session)
        everyone = tokens.holders(session)
    return {
        "tokens": [_view(c, u) for c, u in rows if not status or c.status() == status],
        "holders": [_holder(u) for u in everyone],
    }


@router.post("", status_code=201)
def mint(
    request: Request,
    body: MintRequest,
    current_user: User = Depends(get_current_active_user),
):
    """Issue a token for yourself. It is in this response and nowhere else."""
    _refuse_token_request(request, "mint tokens")
    if _is_service_account(current_user):
        raise HTTPException(
            status_code=403, detail="A service account's token is minted for it"
        )
    _confirm(request, current_user, "create a token")
    minted = tokens.mint(
        current_user.user_id,
        body.label,
        body.expires_in_days,
        actor=current_user.principal_id,
    )
    if minted is None:
        raise HTTPException(status_code=500, detail="Could not mint a token")
    record = minted.record
    _tell(
        current_user,
        "API token issued",
        f'A token "{record.label}" was issued for your account. '
        f"It expires on {_date(record.expires_at)}.",
    )
    return {
        "token": minted.token,
        "credential": _view(record, current_user),
        "warning": "This token is shown once. Store it now; it cannot be recovered.",
    }


@router.post("/{credential_id}/rotate", status_code=201)
def rotate(
    request: Request,
    credential_id: str,
    body: RotateRequest,
    current_user: User = Depends(get_current_active_user),
):
    """Mint a successor with the same holder and label. Only whoever could mint
    for the holder rotates, so an admin revokes another person's token, never this."""
    _refuse_token_request(request, "rotate tokens")
    credential, holder = _found(credential_id)
    if not _may_mint_for(current_user, holder):
        if holder.user_id != current_user.user_id and not _manages(current_user):
            raise HTTPException(status_code=404, detail="Token not found")
        raise HTTPException(
            status_code=403, detail="Only whoever mints for the holder can rotate this"
        )
    _confirm(request, current_user, "rotate a token")
    try:
        rotated = tokens.rotate(
            credential_id,
            body.suspected_compromise,
            body.expires_in_days,
            actor=current_user.principal_id,
        )
    except tokens.NotRotatable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if rotated is None:
        raise HTTPException(status_code=500, detail="Could not rotate the token")
    minted, old = rotated
    ends = (
        "stopped working now"
        if body.suspected_compromise
        else f"stops working on {_date(old.expires_at)}"
    )
    _tell(
        holder,
        "API token rotated",
        f'Your token "{credential.label}" was rotated; the old one {ends}.',
        "high" if body.suspected_compromise else "normal",
    )
    return {
        "token": minted.token,
        "credential": _view(minted.record, holder),
        "rotated": _view(old, holder),
        "warning": "This token is shown once. Store it now; it cannot be recovered.",
    }


@router.delete("/{credential_id}")
def revoke(credential_id: str, current_user: User = Depends(get_current_active_user)):
    """Withdraw a token: its holder's own, or anyone's for a ``users.write`` holder
    (the one act that does not need to cover the holder's role)."""
    credential, holder = _found(credential_id)
    own = holder.user_id == current_user.user_id
    if not own:
        if not _manages(current_user):
            raise HTTPException(status_code=404, detail="Token not found")
        # Held already: this only asks a person to confirm it's them.
        deny_unless(current_user.user_id, USERS_WRITE)
    if not tokens.revoke(credential_id):
        raise HTTPException(status_code=409, detail="Already revoked")
    trail_events.safety(
        "credential.revoked",
        actor=current_user.principal_id,
        principal=holder.principal_id,
        credential_id=credential_id,
    )
    who = current_user.full_name or current_user.username
    _tell(
        holder,
        "API token revoked",
        f'Your token "{credential.label}" was revoked'
        + ("." if own else f" by {who}. Ask them if you were not expecting this."),
        "normal" if own else "high",
    )
    return {"revoked": credential_id}
