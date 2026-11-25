"""API tokens: a person mints and rotates their own, and a ``users.write`` holder
sees every token and revokes any, and mints and rotates a service account's (#4223,
#4224).

A token is a machine caller whoever holds it: it cannot mint, rotate or change
an account. It lives outside ``/api/auth`` so that refusal is this module's own.
Storage and the lifetime rule are ``core.auth.mcp_credential_service``.
"""

import logging
from datetime import timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.auth import break_glass
from core.auth import current_user as current_user_module
from core.auth import directory_sync
from core.auth import mcp_credential_service as tokens
from core.auth import session_policy, step_up, token_notices, token_policy, trail_events
from core.auth.auth_service import _is_service_account
from core.auth.permissions import deny_unless, missing_permission
from core.auth.service_account import is_medic as _is_medic
from core.routing import Auth, RouterMeta
from core.storage.models import McpCredential, OAuthClient, Principal, User
from core.storage.unit_of_work import unit_of_work
from services.api.middleware.auth import get_current_active_user
from services.api.routers.users import _require_privileges_cover_target

logger = logging.getLogger(__name__)
router = APIRouter()

ROUTER_META = RouterMeta(prefix="/api/tokens", tags=["tokens"], auth=Auth.REQUIRED)

USERS_WRITE = "users.write"


class MintRequest(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    # A service account's user id, for a users.write holder; default: yourself.
    user_id: Optional[str] = None
    # None is the default for the holder; the rule is tokens.lifetime.
    expires_in_days: Optional[int] = Field(
        default=None, ge=1, le=tokens.PERSON_MAX_DAYS
    )


class RotateRequest(BaseModel):
    # The old token ends now instead of in 24 hours.
    suspected_compromise: bool = False
    expires_in_days: Optional[int] = Field(
        default=None, ge=1, le=tokens.PERSON_MAX_DAYS
    )


def _refuse_marked(holder: User) -> None:
    """No token for a break-glass account: its sign-in is the safe's codes alone."""
    if holder.break_glass_at is not None:
        raise HTTPException(status_code=403, detail=break_glass.MARKED)


def _holder(user: User) -> dict:
    return {
        "user_id": user.user_id,
        "username": user.username,
        "full_name": user.full_name,
        "service_account": _is_service_account(user),
    }


def _view(
    credential: McpCredential, holder: User, clients: Optional[dict] = None
) -> dict:
    """A token as the lists show it. An OAuth grant (#4789) is a connected app:
    its client's name rides along, and it is revoked, never rotated."""
    app = credential.oauth_client_id is not None
    return {
        **credential.to_dict(),
        "connected_app": app,
        "client_name": (clients or {}).get(credential.oauth_client_id) if app else None,
        "holder": _holder(holder),
    }


def _client_names(session, rows) -> dict:
    """client_id -> name for the grants among ``rows``, in one query."""
    ids = {c.oauth_client_id for c, _ in rows if c.oauth_client_id}
    if not ids:
        return {}
    found = session.query(OAuthClient.client_id, OAuthClient.name).filter(
        OAuthClient.client_id.in_(ids)
    )
    return {cid: name for cid, name in found}


def _tell(holder: User, title: str, message: str, priority: str = "normal") -> None:
    """The holder's recipients are told (core.auth.token_notices). Never the token:
    notices are emailed."""
    token_notices.tell(holder, title, message, priority)


def _limits(holder: User) -> dict:
    """The lifetime that applies to ``holder``'s tokens: the most, the default, and
    whether the holder has no choice (a service account's token gets its maximum)."""
    with unit_of_work() as session:
        stored = session_policy.read_row(session, tolerant=True)[0]
    machine = _is_service_account(holder)
    if stored.unreadable:
        return {"max_days": None, "default_days": None, "fixed": machine}
    return {
        "max_days": token_policy.maximum_days(holder, stored.policy),
        "default_days": tokens.default_days(holder, stored.policy),
        "fixed": machine,
    }


def _unused_rule(record: McpCredential, holder: User) -> Optional[str]:
    """What the unused-token rule says of this new token: when it is revoked if never
    used. None when the rule is off, can't be read, doesn't reach the token (directory
    sync's) or the token ends first."""
    with unit_of_work() as session:
        stored = session_policy.read_row(session, tolerant=True)[0]
    days = None if stored.unreadable else stored.policy.token_unused_days
    if days is None or directory_sync.is_sync_account(holder):
        return None
    ends = record.created_at + timedelta(days=days)
    if record.expires_at is not None and record.expires_at <= ends:
        return None
    return (
        f"A token left unused for {days} days is reminded about, then revoked: if "
        f"this one is never used, on {_date(ends)}. Using it before then keeps it."
    )


def _minted(call):
    """``call()``, with the policy's refusals as responses that state the reason."""
    try:
        return call()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except token_policy.PolicyUnreadable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _refuse_token_request(request: Request, what: str) -> None:
    if current_user_module.is_token_request(request):
        raise HTTPException(status_code=403, detail=f"An API token can't {what}")


def _confirm(request: Request, user: User, action: str) -> None:
    """Creating or rotating a token asks "Confirm it's you" of the person calling,
    whatever their role holds (#4434). A machine caller is never asked."""
    step_up.check(request, user.user_id, action)


def _sign_in(request: Request) -> Optional[str]:
    """The sign-in this request is, for the token's trail entry."""
    claims = getattr(request.state, "access_claims", None)
    sid = claims.get("sid") if isinstance(claims, dict) else None
    return sid or None


def _may_mint_for(caller: User, holder: User) -> bool:
    """A person for themselves, or a person holding users.write for a service
    account that is not Medic's (Medic's is minted from the host only)."""
    if _is_service_account(caller):
        return False
    if caller.user_id == holder.user_id:
        return True
    return _is_service_account(holder) and not _is_medic(holder) and _manages(caller)


def _manages(user: User) -> bool:
    return missing_permission(user.user_id, USERS_WRITE) is None


def _check_holder(caller: User, holder: User) -> None:
    """For a service account: it is live, and its role is one the caller covers."""
    if holder.user_id == caller.user_id:
        return
    with unit_of_work() as session:
        deleted = (
            session.query(Principal.deleted_at)
            .filter(Principal.principal_id == holder.principal_id)
            .scalar()
        )
        if deleted is not None:
            raise HTTPException(status_code=404, detail="User not found")
        _require_privileges_cover_target(
            caller, holder, session, action="mint a token for"
        )
    if not holder.is_active:
        raise HTTPException(
            status_code=409, detail="This account is deactivated; reactivate it first"
        )


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
    """Your own tokens, newest first, revoked and ended ones included, and the
    lifetime that applies to a token of yours."""
    with unit_of_work() as session:
        rows = tokens.list_all(current_user.user_id, session)
        clients = _client_names(session, rows)
    return {
        "tokens": [_view(c, u, clients) for c, u in rows],
        "limits": _limits(current_user),
    }


@router.get("/all")
def list_all(
    holder: Optional[str] = Query(None, description="A holder's user id"),
    status: Optional[Literal["active", "rotated", "expired", "revoked"]] = None,
    current_user: User = Depends(get_current_active_user),
):
    """Every token, for a ``users.write`` holder; filtered on the server.

    ``holders`` is everyone who holds one, whatever the filter, for its menu.
    ``limits`` is the lifetime that applies to the filtered holder's tokens, or yours.
    """
    deny_unless(current_user.user_id, USERS_WRITE)
    with unit_of_work() as session:
        rows = tokens.list_all(holder, session)
        everyone = tokens.holders(session)
        whose = session.get(User, holder) if holder else None
        clients = _client_names(session, rows)
    return {
        "limits": _limits(whose or current_user),
        "tokens": [
            _view(c, u, clients) for c, u in rows if not status or c.status() == status
        ],
        "holders": [_holder(u) for u in everyone],
    }


@router.post("", status_code=201)
def mint(
    request: Request,
    body: MintRequest,
    current_user: User = Depends(get_current_active_user),
):
    """Issue a token for yourself, or for a service account if you hold users.write.
    It is in this response and nowhere else."""
    _refuse_token_request(request, "mint tokens")
    if _is_service_account(current_user):
        raise HTTPException(
            status_code=403, detail="A service account's token is minted for it"
        )
    holder = current_user
    if body.user_id and body.user_id != current_user.user_id:
        with unit_of_work() as session:
            holder = session.get(User, body.user_id)
        # Before the lookup answers anything: only a users.write holder learns more.
        deny_unless(current_user.user_id, USERS_WRITE)
        if holder is None or not _is_service_account(holder):
            raise HTTPException(status_code=404, detail="Service account not found")
        if _is_medic(holder):
            raise HTTPException(
                status_code=403, detail="Medic's token is minted from the host only"
            )
        if directory_sync.is_sync_account(holder):
            raise HTTPException(
                status_code=403,
                detail="Directory sync's token is minted from Single sign-on",
            )
        _check_holder(current_user, holder)
    _refuse_marked(holder)
    _confirm(request, current_user, "create a token")
    minted = _minted(
        lambda: tokens.mint(
            holder.user_id,
            body.label,
            body.expires_in_days,
            actor=current_user.principal_id,
            sign_in=_sign_in(request),
        )
    )
    if minted is None:
        raise HTTPException(status_code=500, detail="Could not mint a token")
    record = minted.record
    whose = (
        f"service account {holder.username}"
        if holder is not current_user
        else "your account"
    )
    unused = _unused_rule(record, holder)
    _tell(
        holder,
        "API token issued",
        f'A token "{record.label}" was issued for {whose}. '
        f"It expires on {_date(record.expires_at)}." + (f" {unused}" if unused else ""),
    )
    return {
        "token": minted.token,
        "credential": _view(record, holder),
        "unused_rule": unused,
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
    if directory_sync.is_sync_account(holder):
        raise HTTPException(
            status_code=403,
            detail="Directory sync's token is minted from Single sign-on",
        )
    if credential.oauth_client_id and holder.user_id == current_user.user_id:
        # Its tokens rotate through the app's refresh; a vgl_api_ successor
        # would hand the app a token for the whole API.
        raise HTTPException(
            status_code=409,
            detail="An app's connection isn't rotated; revoke it and connect again",
        )
    if not _may_mint_for(current_user, holder):
        if holder.user_id != current_user.user_id and not _manages(current_user):
            raise HTTPException(status_code=404, detail="Token not found")
        raise HTTPException(
            status_code=403, detail="Only whoever mints for the holder can rotate this"
        )
    _check_holder(current_user, holder)
    _refuse_marked(holder)
    _confirm(request, current_user, "rotate a token")
    try:
        rotated = _minted(
            lambda: tokens.rotate(
                credential_id,
                body.suspected_compromise,
                body.expires_in_days,
                actor=current_user.principal_id,
                sign_in=_sign_in(request),
            )
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
    whose = (
        f"service account {holder.username}'s"
        if _is_service_account(holder)
        else "Your"
    )
    unused = _unused_rule(minted.record, holder)
    _tell(
        holder,
        "API token rotated",
        f'{whose} token "{credential.label}" was rotated; the old one {ends}.'
        + (f" {unused}" if unused else ""),
        "high" if body.suspected_compromise else "normal",
    )
    return {
        "token": minted.token,
        "credential": _view(minted.record, holder),
        "unused_rule": unused,
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
        (
            f"The token of service account {holder.username}"
            if _is_service_account(holder)
            else "Your token"
        )
        + f' "{credential.label}" was revoked'
        + ("." if own else f" by {who}. Ask them if you were not expecting this."),
        "normal" if own else "high",
    )
    return {"revoked": credential_id}
