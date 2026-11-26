"""
Authentication API - User authentication endpoints.

Handles login, logout, token refresh, password management, and MFA.
"""

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, List, NamedTuple, Optional, Union
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, text
from sqlalchemy.orm import Session, object_session

from core.audit.retention import ERASED_PREFIX, is_erased
from core.audit.trail import SYSTEM
from core.auth import (
    break_glass,
    directory_sync,
    invites,
    key_ring,
    mfa_methods,
    password_hash,
    principals,
    security_keys,
    session_policy,
    sign_ins,
    sso,
    sso_settings,
    step_up,
    trail_events,
)
from core.auth.auth_cookies import (
    ACCESS_COOKIE_NAME,
    REFRESH_COOKIE_NAME,
    _cookie_secure,
    clear_auth_cookies,
    context_path_prefix,
    set_auth_cookies,
)
from core.auth.auth_service import (
    CODE_APP,
    CODE_KEY,
    CODE_RECOVERY,
    PASSWORD_RESET_REQUIRED,
    AccountLockedError,
    AuthService,
    _account_is_locked,
    _is_service_account,
    clear_lock,
    managers,
    password_matches_any,
    raise_if_locked,
    with_dispute,
)
from core.auth.bootstrap_code import has_human_user, setup_code_matches
from core.auth.current_user import (
    DEV_MODE,
    _check_sign_in,
    _detached,
    mfa_setup_route,
    setup_needed,
)
from core.auth.deactivation import when_committed
from core.auth.identity import (
    RESERVED,
    UNAVAILABLE,
    claim_sign_in_names,
    fold,
    identifier_in_use,
    matching_accounts,
)
from core.auth.mcp_credential_service import looks_like_api_token
from core.auth.password_reset import default_base_url, reset_link
from core.auth.password_validator import (
    USERS_LINK,
    PasswordPolicyError,
    changeable_at,
    check_not_expired,
    check_not_listed,
    validate_password_strength,
)
from core.config import get_settings
from core.platform.email_service import mail_is_configured, send_email
from core.platform.notices import SIGN_IN_LIMIT_KIND, notify
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import Principal, Role, SecurityKey, SignIn, SsoLink, User
from core.storage.schemas import UserSchema
from core.storage.schemas.base import IsoDateTime, OptDateTime
from core.storage.unit_of_work import unit_of_work
from core.teams import join_default
from core.time import utcnow
from services.api.middleware.auth import get_current_active_user
from services.api.middleware.auth_rate_key import (
    bootstrap_rate_key,
    confirm_email_rate_key,
    invite_rate_key,
    login_rate_key,
    refresh_rate_key,
    reset_confirm_rate_key,
    reset_request_rate_key,
    session_rate_key,
    sso_rate_key,
)
from services.api.middleware.client_address import client_host
from services.api.middleware.rate_limit import limiter

logger = logging.getLogger(__name__)

# A service account (Medic's gateway, D2-17b) may log in and refresh through the
# API and nothing else here, so a leaked password can't become a console session.
SERVICE_ACCOUNT_DETAIL = "Service accounts can't use this endpoint"
# One answer for everyone but managers: not which of the two it was.
_IN_USE_OR_RESERVED = "That username or email is in use or reserved. Choose another."
_INVALID_REFRESH_TOKEN = "Invalid refresh token"
_INVALID_RESET_TOKEN = "Invalid or expired reset token"
# The 400 body's code when the minimum password age refuses a change.
PASSWORD_MIN_AGE = "password_min_age"
# Login's 403 detail.code when the password must change before sign-in.
PASSWORD_CHANGE_REQUIRED = "password_change_required"
# A sign-in without MFA rechecks a password shorter than the lower of this and
# the policy minimum. Out of the box the minimum is 16, so this stays 15.
SINGLE_FACTOR_MIN = 15


def _bearer_token(request: Request) -> Optional[str]:
    parts = (request.headers.get("authorization") or "").split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


async def _refuse_service_account(request: Request):
    """Refuse a service account's token on every route but login and refresh.

    On the whole router, so a route added later can't leave it out. It never
    authenticates: a missing or invalid token passes, and the route's own
    dependency (or none, for the public routes) decides as before. Both the
    access cookie and the bearer are checked, whichever the route would use.
    The header is read off the request so it stays out of the OpenAPI schema.
    Its read is its own short session, like ``get_current_user``'s: the
    request's would hold a connection while authentication takes another.
    """
    if request.scope.get("endpoint") in _SERVICE_ACCOUNT_ENDPOINTS:
        return
    for raw in {request.cookies.get(ACCESS_COOKIE_NAME), _bearer_token(request)}:
        payload = AuthService.verify_jwt_token(raw) if raw else None
        user_id = payload.get("user_id") if payload else None
        if not user_id:
            continue
        user = await asyncio.to_thread(_detached, lambda db: _get_user(db, user_id))
        if user is not None and _is_service_account(user):
            await asyncio.to_thread(
                trail_events.refused,
                "auth.denied",
                {"source_ip": client_host(request)},
                actor=user.principal_id,
                attempted=request.url.path,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=SERVICE_ACCOUNT_DETAIL
            )


TOKEN_DETAIL = "An API token can't use this endpoint"


def _refuse_token(request: Request):
    """Refuse an API token on every route here, so a token can't change its holder's
    password, MFA or email, or mint tokens. On the whole router for the same reason as
    above. It goes by the header alone, valid or not: no route here answers a token."""
    token = _bearer_token(request)
    if token and looks_like_api_token(token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=TOKEN_DETAIL)


router = APIRouter(
    dependencies=[Depends(_refuse_token), Depends(_refuse_service_account)]
)

ROUTER_META = RouterMeta(
    prefix="/api/auth",
    tags=["authentication"],
    auth=Auth.ROUTER_MANAGED,
    reason=(
        "A deliberate mix. login / refresh / password-reset / bootstrap "
        "cannot require auth (chicken-and-egg); the inner /me, "
        "/change-password and /mfa routes "
        "declare get_current_active_user inline. A router-level auth "
        "dependency here would break login."
    ),
)


def _is_browser_request(request: Request) -> bool:
    """Whether a page in a browser sent this, rather than a program.

    A browser always sends ``Origin`` on a cross- or same-origin POST from page
    JS (fetch or a form), and current browsers add ``Sec-Fetch-Site`` to every
    request; page JS can remove neither. Medic's gateway sends neither, so its
    login is unchanged. This is only ever used to refuse: a client that forges
    one of these headers can only make its own login fail.
    """
    return "origin" in request.headers or "sec-fetch-site" in request.headers


def _invalid_credentials() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid username/email or password",
        headers={"WWW-Authenticate": "Bearer"},
    )


# The role the first account gets: it has to be able to create every other one.
ADMIN_ROLE_ID = "role-admin"

# Arbitrary constant identifying the bootstrap advisory lock.
_BOOTSTRAP_LOCK = 8_274_119


# Request/Response Models
class BootstrapStatusResponse(BaseModel):
    """Whether the instance still needs its first account."""

    required: bool
    # Single sign-on is on: the login page offers "Sign in with SSO".
    sso_enabled: bool = False
    # Require single sign-on is in force: the page offers only "Sign in with SSO".
    require_sso: bool = False


class BootstrapRequest(BaseModel):
    """First-admin details."""

    username: str
    email: EmailStr
    password: str
    full_name: Optional[str] = None
    # Optional so a missing code is a 403 from the handler, not a 422.
    setup_code: Optional[str] = None


class LoginRequest(BaseModel):
    """Login request."""

    username_or_email: str
    password: str
    mfa_code: Optional[str] = None
    # The second call with a security key: the key's assertion and the signed
    # challenge the first call's 401 carried.
    webauthn: Optional[dict] = None
    webauthn_challenge: Optional[str] = None
    # A break-glass account's reason, sent with its code (#4532).
    reason: Optional[str] = None


class LoginResponse(BaseModel):
    """Login response."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: dict
    # Set once, at the first MFA sign-in after the upgrade: the old recovery
    # codes are void and these replace them.
    recovery_codes: Optional[List[str]] = None
    # A recovery code got this sign-in in: every method is gone, and the console
    # goes straight to setting one up.
    mfa_removed: bool = False


class ChangePasswordRequest(BaseModel):
    """Change password request."""

    current_password: str
    new_password: str
    # The person suspects the current password has leaked: the minimum age is
    # waived, the trail records it and the managers are told.
    suspected_leak: bool = False


class ProfileUpdateRequest(BaseModel):
    """Signed-in user's own profile.

    A different ``email`` always takes the strong check, inside the confirmation
    window too: ``current_password`` and, with MFA on, ``code``. They are body
    fields so they are not written to access logs.
    """

    full_name: Optional[str] = None
    email: Optional[EmailStr] = None
    current_password: Optional[str] = None
    code: Optional[str] = None


class RefreshTokenRequest(BaseModel):
    """Refresh token request.

    refresh_token is optional because the primary transport is now the
    HttpOnly refresh_token cookie. The body field stays for API/CLI
    clients that haven't migrated to the cookie flow.
    """

    refresh_token: Optional[str] = None


class MFASetupResponse(BaseModel):
    """MFA setup response."""

    secret: str
    qr_uri: str


class RecoveryCodesResponse(BaseModel):
    """One-time MFA recovery codes (shown once, cannot be retrieved again)."""

    recovery_codes: List[str]
    message: Optional[str] = None


class MFAVerifyRequest(BaseModel):
    """A method change: the code and, for the strong check, the password. Body
    fields so neither is written to access logs. Optional so a missing password
    or code is the check's own "Enter your password.", not a 422. A person with
    security keys may send ``assertion`` and ``challenge`` in place of the code
    (from POST /mfa/keys/assertion-options)."""

    code: Optional[str] = None
    password: Optional[str] = None
    assertion: Optional[dict] = None
    challenge: Optional[str] = None
    # In place of the password for a person who signs in only through the
    # provider: the proof GET /sso/start?confirm=…&method=1 hands back (#4756).
    sso_proof: Optional[str] = None


class PasswordResetRequest(BaseModel):
    """Password reset initiation."""

    email: EmailStr


class PasswordResetConfirm(BaseModel):
    """Password reset completion."""

    token: str
    new_password: str


def _apply_new_password(user: User, plaintext: str) -> dict:
    """Enforce history, hash, set, update history + changed_at in one place.
    The request's unit of work owns the commit. Entries under a retired pepper
    can't be read: the history check skips them, and recovery codes among them are
    removed. Returns what that skipped, for the trail."""
    keep = session_policy.policy().password_history
    checked = [user.password_hash, *(user.password_history or [])[:keep]]
    # The current hash and the history are checked together (PBKDF2 runs once
    # per distinct salt).
    if password_matches_any(plaintext, checked):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Password matches your current password or one of your "
                f"previous {keep} — choose a different one."
            ),
        )

    new_hash = AuthService.hash_password(plaintext)
    previous = list(user.password_history or [])
    if user.password_hash:
        previous.insert(0, user.password_hash)
    # Cap to the policy's history so the JSONB row doesn't grow unbounded;
    # lowering it trims here, at the next change.
    user.password_history = previous[:keep]
    user.password_hash = new_hash
    user.password_changed_at = utcnow()
    # A new password closes every reset link still open (a confirm's own is used).
    invites.revoke_resets(
        object_session(user), user.principal_id, user.principal_id, "password_changed"
    )
    skipped = sum(password_hash.unreadable(h) for h in checked)
    codes_removed = any(
        password_hash.unreadable(c) for c in user.mfa_recovery_codes or []
    )
    if codes_removed:  # new ones come from the existing regeneration flow
        user.mfa_recovery_codes = []
    return {"history_skipped": skipped, "recovery_codes_removed": codes_removed}


def _min_age_refusal(user: User) -> Optional[JSONResponse]:
    """The 400 that says when a voluntary change is next allowed, or None now."""
    at = changeable_at(user, session_policy.policy().password_min_age_days)
    if at is None:
        return None
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "code": PASSWORD_MIN_AGE,
            "detail": (
                "Your password was changed too recently. Another change is allowed "
                f"from {at:%Y-%m-%d %H:%M} UTC."
            ),
            "changeable_at": at.isoformat() + "Z",
        },
    )


def _session_sid(request: Request) -> Optional[str]:
    """The sign-in the request's own token names, for its trail entry."""
    raw = request.cookies.get(ACCESS_COOKIE_NAME) or _bearer_token(request)
    payload = AuthService.verify_jwt_token(raw) if raw else None
    return payload.get("sid") if payload else None


def _get_user(session: Session, user_id: str, lock: bool = False) -> Optional[User]:
    query = session.query(User).filter(User.user_id == user_id)
    return (query.with_for_update() if lock else query).first()


def _limits(user: User, session: Session) -> session_policy.Limits:
    """The session limits for this account, by the permissions it holds.

    An idle of 0 is no idle limit: ``limits`` returns None, and a refresh does
    not end the sign-in for idleness.
    """
    held = AuthService.get_user_permissions(user.user_id, session)
    return session_policy.limits(
        [name for name, on in held.items() if on], _is_service_account(user)
    )


class _Ended(NamedTuple):
    """A refresh refused because its sign-in has ended, and why."""

    reason: Optional[str]


def _ended(reason: Optional[str]) -> JSONResponse:
    """The 401 for an ended sign-in; the reason rides in a header the console reads."""
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": "Sign-in has ended"},
        headers={sign_ins.END_REASON_HEADER: reason} if reason else None,
    )


def _continue_sign_in(
    session: Session,
    sid: str,
    generation: int,
    user: User,
    user_agent: Optional[str],
) -> Union[tuple[str, str, dict], str, _Ended]:
    """Continue the sign-in a refresh names: new tokens and the user payload,
    or the reason it is refused. Synchronous end to end: see the caller.

    The row is locked so a concurrent logout or refresh waits. A refresh token
    carries the generation it was minted at: the current one advances it, the
    one before it is still honoured for 10 s after (a second tab), and anything
    older is a replay and ends the sign-in.
    """
    row = session.get(SignIn, sid, with_for_update=True)
    if row is None or row.ended_at is not None or row.principal_id != user.principal_id:
        return _Ended(row.end_reason if row is not None else None)
    # A browser mismatch refuses this request and ends nothing.
    if row.user_agent and row.user_agent != (user_agent or ""):
        return _INVALID_REFRESH_TOKEN
    limit = _limits(user, session)
    now = utcnow()
    if sign_ins.end_of(row, limit.absolute) <= now:
        sign_ins.end(sid, sign_ins.LENGTH_LIMIT, session)
        return _Ended(sign_ins.LENGTH_LIMIT)
    if limit.idle and row.last_action_at + limit.idle <= now:
        sign_ins.end(sid, sign_ins.IDLE, session)
        return _Ended(sign_ins.IDLE)

    current = row.refresh_generation
    if generation == current:
        row.refresh_generation = current = current + 1
        row.refresh_rotated_at = now
    elif not (
        generation == current - 1
        and row.refresh_rotated_at is not None
        and now - row.refresh_rotated_at <= sign_ins.REFRESH_GRACE
    ):
        sign_ins.end(sid, sign_ins.TOKEN_REUSE, session)
        return "Refresh token has already been used"
    # A new sign-in made this way would be refused. After the reuse check, so a
    # replay still ends as token reuse. Joins this transaction, which commits
    # with the 401.
    if step_up.non_sso_now_refused(user, row.auth_method):
        sign_ins.end(sid, sign_ins.SSO_REQUIRED, session)
        return _Ended(sign_ins.SSO_REQUIRED)

    access_token = AuthService.generate_jwt_token(user, "access", sid=sid)
    refresh_token = AuthService.generate_jwt_token(
        user,
        "refresh",
        sid=sid,
        generation=current,
        not_after=sign_ins.end_of(row, limit.absolute),
    )
    refresh_payload = AuthService.verify_jwt_token(refresh_token) or {}
    row.expires_at = _exp_datetime(refresh_payload.get("exp"))
    return access_token, refresh_token, _user_payload(user, session, sid)


def _user_payload(user: User, session: Session, sid: Optional[str] = None) -> dict:
    """User dict plus resolved permissions — the shape the SPA gates on.
    Login/refresh must include it, not just /me: the client stores the user
    from the login response and checks permissions before any /me refresh.
    ``sid`` is the sign-in asking: how it met MFA decides a hold at setup."""
    row = session.get(SignIn, sid) if sid else None
    payload = UserSchema.dump(user)
    payload["permissions"] = AuthService.get_user_permissions(user.user_id, session)
    # The console says before a save what a machine is refused (#5276).
    payload["service_account"] = getattr(user, "service_account", False) is True
    # The console holds this person at MFA setup until it is done.
    # (Not in dev mode, which has no gate and no user row to set MFA up on.)
    payload["mfa_setup_required"] = not DEV_MODE and bool(
        setup_needed(
            session,
            user,
            [name for name, on in payload["permissions"].items() if on],
            session_policy.policy(),
            row,
        )
    )
    # Whether this install has security keys, and why not when it has none.
    payload["security_keys"] = security_keys.availability().dump()
    # Signs in only through the provider: confirms there, not with a password,
    # before a method change (#4756).
    payload["sso_only"] = sso.sso_only(session, user)
    payload["sso_provider"] = step_up._provider_name() if payload["sso_only"] else None
    return payload


@router.get("/bootstrap", response_model=BootstrapStatusResponse)
def bootstrap_status(session: UnitOfWorkSession):
    """Report whether this instance has no account yet.

    There is no self-service signup and creating a user needs users.write, so
    an instance with an empty user table cannot be signed into at all. The
    login screen asks this to offer first-account creation instead.
    """
    s = sso_settings.settings()
    return BootstrapStatusResponse(
        required=not has_human_user(session),
        sso_enabled=sso.is_on(s),
        require_sso=sso.required(s),
    )


@router.post("/bootstrap", status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute", key_func=bootstrap_rate_key)
def bootstrap_admin(
    request: Request,
    payload: BootstrapRequest,
    session: UnitOfWorkSession,
):
    """Create the first admin account. Only ever available on an empty instance.

    Unauthenticated by necessity — it is the only way to obtain the account
    every other user-creating path requires. It closes permanently as soon as
    one user exists, so it is not a signup endpoint. The body must include the
    setup code the API logs at startup while the user table is empty.
    """
    # Serialize bootstrap attempts. Without this two callers both read an empty
    # table and both create an admin; there is no row yet to lock instead. The
    # lock is held to the end of the request's transaction, so it covers
    # check-and-create.
    session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": _BOOTSTRAP_LOCK}
    )

    if has_human_user(session):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="An account already exists. Ask an administrator to create yours.",
        )

    # Inside the advisory lock, before any user row is written. The submitted
    # value is not logged: a wrong guess must not land in the log stream.
    if not setup_code_matches(payload.setup_code):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Setup code is missing or incorrect.",
        )

    try:
        validate_password_strength(
            payload.password,
            user_inputs=[payload.username, payload.email],
        )
    except PasswordPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=exc.as_detail()
        )

    user = AuthService.create_user(
        username=payload.username,
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name or payload.username,
        role_id=ADMIN_ROLE_ID,
        session=session,
    )
    if not user:
        # None means the username or email is in use or reserved.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=_IN_USE_OR_RESERVED
        )

    trail_events.record(
        session,
        "user.created",
        {"personal": trail_events.personal(user)},
        actor=SYSTEM,
        principal=user.principal_id,
        role_id=user.role_id,
    )
    logger.info("First admin account created: %s", user.username)
    return UserSchema.dump(user)


def _end_others_after_change(
    session: Session,
    user: User,
    keep: Optional[str],
    source: Optional[str],
    reason: str = sign_ins.ACCOUNT_CHANGED,
) -> None:
    """A method change ends the person's other sign-ins and stops their runs;
    ``keep``, the one making the change, stays open with its runs.

    ``reason`` stays ``account_changed`` for a role, code or method change.
    A recovery-code sign-in passes ``recovery``.
    """
    ids = [sid for sid, _ in sign_ins.open_ids(session, user.principal_id, keep=keep)]
    sign_ins.end_all_for_user(
        user.user_id,
        reason,
        session,
        except_sign_in_id=keep,
    )
    for sid in ids:
        trail_events.record(
            session,
            "auth.sign_in_ended",
            {"source_ip": source},
            actor=user.principal_id,
            principal=user.principal_id,
            sign_in=sid,
            end_reason=reason,
        )


def _tell_person(
    session: Session,
    user: User,
    title: str,
    message: str,
    kind: str = "mfa_change",
    fallback: str = "",
) -> None:
    """Tell the person once the change commits. Never a code: notices are emailed.

    ``fallback`` is the dispute sentence already in ``message``. It is left as
    written when the person has no other account manager."""
    message = with_dispute(user.principal_id, message, fallback, session)
    principal = user.principal_id

    def send() -> None:
        notify(principal, kind, title, message, link="/settings", priority="high")

    when_committed(session, send)


# The dispute sentences already on the notices. Replaced only when the person
# has another account manager to name.
_DISPUTE_PASSWORD = "If this wasn't you, change your password and tell an admin."
_DISPUTE_NOW = "If this wasn't you, tell an admin now."
_DISPUTE_SHORT = "If this wasn't you, tell an admin."
_DISPUTE_SIGN_OUT = (
    "If this wasn't you, sign out everywhere else from your sign-ins in the "
    "console, change your password and tell an admin."
)
_NO_CONFIRM_LINK = (
    "No confirmation link could be sent. The address stays unconfirmed until "
    "mail is configured and the install's public address is set."
)


def _confirm_mail_ready() -> bool:
    """The same test an emailed invite uses: mail, and a public console address."""
    return bool(mail_is_configured() and get_settings().vigil_frontend_url.strip())


def _change_notice(
    session: Session, user: User, what: str, source: Optional[str]
) -> str:
    """What changed, when, and the address the request came from. No link."""
    at = f"{utcnow():%Y-%m-%d %H:%M} UTC"
    where = source if source else "an unknown address"
    return f"{what} at {at} from {where}. {_DISPUTE_SHORT}"


def _send_confirm_link(session: Session, user: User, token: str) -> None:
    """Mail the confirmation link once the change commits. The value is only here."""
    link = invites.confirm_link(get_settings().vigil_frontend_url.strip(), token)
    to = user.email
    name = user.full_name or user.username
    body = (
        f"Hello {name},\n\n"
        "Confirm this address for your Vigil account. Open the link below. "
        "It is valid for 24 hours and can only be used once.\n\n"
        f"{link}\n\n"
        "If you did not change your email, you can ignore this email."
    )

    def send() -> None:
        send_email(to=to, subject="Confirm your Vigil email address", body=body)

    when_committed(session, send)


def _tell_address(session: Session, address: str, title: str, message: str) -> None:
    """The same notice, mailed to an address that is no longer on the account."""

    def send() -> None:
        send_email(to=address, subject=title, body=message)

    when_committed(session, send)


def _recover(session: Session, user: User, source: Optional[str]) -> None:
    """A recovery code was accepted at login: take every method, end the other
    sign-ins, and tell the person."""
    AuthService.clear_mfa(user)
    security_keys.remove_all(session, user.principal_id)
    trail_events.record(
        session,
        "auth.mfa_recovered",
        {"source_ip": source},
        actor=user.principal_id,
        principal=user.principal_id,
    )
    _end_others_after_change(session, user, None, source, sign_ins.RECOVERY)
    _tell_person(
        session,
        user,
        "A recovery code was used to sign in",
        "A recovery code was used to sign in to your account. Your authenticator "
        "app and remaining recovery codes were removed and your other sign-ins "
        "ended; two-step sign-in has to be set up again. If this wasn't you, "
        "change your password and tell an admin.",
        fallback=_DISPUTE_PASSWORD,
    )


def _recovery_step_down(session: Session, user: User, source: Optional[str]) -> None:
    """A key-only person gave a recovery code while keys are unavailable: nothing is
    removed and no other sign-in ends. They are told, and so are the managers."""
    trail_events.record(
        session,
        "auth.recovery_code_used",
        {"source_ip": source},
        actor=user.principal_id,
        principal=user.principal_id,
        step_down="recovery_code",
    )
    _tell_person(
        session,
        user,
        "A recovery code was used to sign in",
        "A recovery code was used to sign in to your account because security keys "
        "are unavailable. The code is spent; nothing else was removed. If this "
        "wasn't you, change your password and tell an admin.",
        fallback=_DISPUTE_PASSWORD,
    )
    principal, name = user.principal_id, user.username

    def tell_managers() -> None:
        for to in managers(principal):
            try:
                notify(
                    to,
                    "mfa_change",
                    f"{name} signed in with a recovery code",
                    f"{name} has only security keys and signed in with a recovery "
                    "code because keys are unavailable on this install.",
                    USERS_LINK,
                    priority="high",
                    about=principal,
                )
            except Exception:  # noqa: BLE001 - the rest are still told
                logger.exception("could not send a recovery notice for %s", name)

    when_committed(session, tell_managers)


def _break_glass_used(
    session: Session, user: User, reason: str, source: Optional[str], factor: str
) -> None:
    """A break-glass account's second factor was accepted: on the trail with its
    reason, and one notice to each manager once committed."""
    trail_events.record(
        session,
        "auth.break_glass_used",
        {"source_ip": source, "personal": {"reason": reason}},
        actor=user.principal_id,
        principal=user.principal_id,
        factor=factor,
    )
    principal, name = user.principal_id, user.username
    left = len(user.mfa_recovery_codes or [])
    where = source or "an unknown address"
    message = (
        f"{name}, a break-glass account, signed in from {where}. "
        f"Reason given: {reason}. "
        f"{left} recovery code{'' if left == 1 else 's'} left."
    )

    def tell_managers() -> None:
        for to in managers(principal):
            try:
                notify(
                    to,
                    "break_glass_used",
                    f"Break-glass sign-in: {name}",
                    message,
                    USERS_LINK,
                    priority="high",
                    about=principal,
                )
            except Exception:  # noqa: BLE001 - the rest are still told
                logger.exception("could not send a break-glass notice for %s", name)

    when_committed(session, tell_managers)


def _mfa_required(
    session: Session, user: User, key_required: bool = False, marked: bool = False
) -> JSONResponse:
    """The first login call's 401 for a person with MFA: ``X-MFA-Required`` as
    ever, and with security keys the request options and signed challenge for the
    second call. Nothing of this attempt is kept: it is not a sign-in yet."""
    if user in session:
        session.expire(user)
    keyed = security_keys.has_keys(session, user.principal_id)
    methods = ["authenticator_app"] if mfa_methods.has_app(user, keyed) else []
    # Where keys are required and work, the app no longer counts for a person with one.
    if keyed and key_required and security_keys.availability().available:
        methods = []
    detail, challenge = "MFA code required", None
    if keyed:
        methods.append("security_key")
        try:
            challenge = security_keys.assertion_options(
                session, user, security_keys.SIGN_IN
            )
        except security_keys.KeyRefused as exc:
            # Keys are off for this install: the other method still works.
            detail = str(exc) if methods == ["security_key"] else detail
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={
            "detail": detail,
            "methods": methods,
            "security_key": challenge,
            # The console asks for a code and a reason together.
            "break_glass": marked,
        },
        headers={"X-MFA-Required": "true"},
    )


class _Factor(NamedTuple):
    """What a second step that held came to."""

    # Set when security keys are required of this person: how the sign-in got in
    # while keys are unavailable (a step-down), or that the app alone did not count.
    step_down: Optional[str]
    app_not_counted: bool
    # A recovery code took every method: the console goes straight to setup.
    recovered: bool
    new_codes: Optional[List[str]]


def _second_factor(
    session: Session,
    user: User,
    code: Optional[str],
    webauthn: Optional[dict],
    challenge: Optional[str],
    key_required: bool,
    source: Optional[str],
    failed,
    marked: bool = False,
    reason: str = "",
) -> _Factor:
    """The sign-in's second step, for a password and for single sign-on alike:
    the app, a key or a recovery code, a wrong one counted toward the lock, the
    key rule with its step-down, and recovery. Raises 401 when it does not hold;
    ``failed`` puts the refusal on the trail. The lock itself is the caller's to
    clear once the sign-in goes ahead."""
    if user in session:
        session.expire(user)
    step_down: Optional[str] = None
    app_not_counted = recovered = False
    new_codes = None
    # The login step is the one place a wrong code counts toward a lock.
    if webauthn:
        try:
            # The lock is authoritative, as it is for a code.
            if _account_is_locked(user):
                raise security_keys.KeyRefused("locked")
            security_keys.verify_assertion(
                session, user, webauthn, challenge, security_keys.SIGN_IN
            )
            accepted = CODE_KEY
        except security_keys.KeyRefused as exc:
            accepted = None
            # A key switched off by configuration is not a wrong key.
            if not isinstance(exc, security_keys.KeysUnavailable):
                AuthService._count_mfa_failure(user.user_id, user.username, source)
    else:
        accepted = AuthService.verify_mfa_code(
            user.user_id, code or "", session, count_failures=True, source=source
        )
    if not accepted:
        failed("bad_security_key" if webauthn else "bad_mfa_code")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid security key" if webauthn else "Invalid MFA code",
        )
    keyed = security_keys.has_keys(session, user.principal_id)
    keys_up = security_keys.availability().available
    if key_required and accepted == CODE_APP:
        if keyed and keys_up:
            failed("security_key_required")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="A security key is required to sign in",
            )
        # Keys down: the app stands in, marked. Keys up and none held: the
        # sign-in goes on to setup, but the app does not open the window.
        if keys_up:
            app_not_counted = True
        else:
            step_down = "app"
    key_only = keyed and not mfa_methods.has_app(user, keyed)
    if marked:
        # A break-glass factor: a code is spent and nothing else changes; the
        # host's codes are never replaced here.
        _break_glass_used(
            session,
            user,
            reason,
            source,
            {CODE_APP: "authenticator_app", CODE_KEY: "security_key"}.get(
                accepted, "recovery_code"
            ),
        )
    elif accepted == CODE_RECOVERY and key_required and key_only and not keys_up:
        # The one exception to recovery: with keys unavailable the code is
        # spent and nothing else changes, so the person can add the app.
        step_down = "recovery_code"
        _recovery_step_down(session, user, source)
    elif accepted == CODE_RECOVERY:
        # Recovery (#4233): the code is spent, every method and the rest of
        # the codes go, the other sign-ins end with their runs, and this one
        # carries on into setup. An old-scheme code only upgrades the set.
        _recover(session, user, source)
        recovered = True
    # Whether TOTP or an old code got in, an old-scheme set ends here.
    if not marked:
        new_codes = AuthService.upgrade_recovery_codes(user)
    return _Factor(step_down, app_not_counted, recovered, new_codes)


# Two evictions of one account within this long tell its managers.
EVICTION_WINDOW = timedelta(hours=1)
EVICTION_TEXT = (
    "A new sign-in took the place of your oldest one, because your account is "
    "at its sign-in limit. If this wasn't you, change your password and tell an admin."
)


def _tell_of_eviction(
    session: Session, user: User, sid: str, ua: Optional[str], source: Optional[str]
) -> None:
    """After the commit, tell the person whose oldest sign-in the new one ended,
    and, when it makes the account's second eviction in the hour, its managers
    (a third or later tells nobody). A failed send never touches the login."""
    # One login's evictions share an ``ended_at``, so distinct values count logins.
    evictions = (
        session.query(func.count(func.distinct(SignIn.ended_at)))
        .filter(
            SignIn.principal_id == user.principal_id,
            SignIn.end_reason == sign_ins.SIGN_IN_LIMIT,
            SignIn.ended_at > utcnow() - EVICTION_WINDOW,
        )
        .scalar()
    )
    principal, name = user.principal_id, user.username
    # The stored notice is generic: the panel reads the sign-in's row, which erasure
    # can blank. The mailed copy says where it came from.
    where = f"{sign_ins.browser_of(ua)} ({sign_ins.device_of(ua)})"
    if source:
        where += f", address {source}"
    text = with_dispute(principal, EVICTION_TEXT, _DISPUTE_PASSWORD, session)

    def send() -> None:
        try:
            notify(
                principal,
                SIGN_IN_LIMIT_KIND,
                "A new sign-in ended your oldest one",
                text,
                link="/overview?sign_ins=1",
                priority="high",
                metadata={"sign_in_id": sid},
                mail_body=f"{text} It came from {where}.",
            )
        except Exception:  # noqa: BLE001 - the managers are still told
            logger.exception("could not send a sign-in limit notice for %s", name)
        if evictions != 2:
            return
        for to in managers(principal):
            try:
                notify(
                    to,
                    "sign_in_limit_repeat",
                    f"{name}'s sign-ins were ended twice in an hour",
                    f"A new sign-in ended another of {name}'s under the account's "
                    "sign-in limit for the second time within an hour. It may be "
                    "someone else signing in with their credentials.",
                    USERS_LINK,
                    priority="high",
                    about=principal,
                )
            except Exception:  # noqa: BLE001 - the rest are still told
                logger.exception("could not send a sign-in limit notice for %s", name)

    when_committed(session, send)


@router.post("/login", response_model=LoginResponse)
@limiter.limit("5/minute", key_func=login_rate_key)
def login(
    request: Request,
    response: Response,
    payload: LoginRequest,
    session: UnitOfWorkSession,
):
    """
    Authenticate user and issue tokens.

    Tokens are delivered two ways:
    - **HttpOnly cookies** (primary transport for the browser UI) — not
      readable from JavaScript, protected against XSS exfiltration.
    - **Response body** (for CLI/API clients) — these continue to send
      the token as `Authorization: Bearer …`.

    Args:
        request: FastAPI request (used by the rate limiter).
        response: FastAPI response, used to set auth cookies.
        payload: Login credentials
        session: Database session

    Returns:
        Access and refresh tokens with user info
    """
    source = client_host(request)
    refusal: dict = {}

    def failed(reason: Optional[str] = None) -> None:
        # Its own transaction: the 401 or 423 rolls the request's back.
        trail_events.sign_in_failed(
            source,
            payload.username_or_email,
            reason or refusal.get("reason"),
            refusal.get("principal"),
        )

    # Require single sign-on: refuse before the password is checked, so it neither
    # counts toward a lock nor tells a right password from a wrong one. Only a
    # service account goes on, and not from a browser, where it is refused anyway:
    # there every name, known or not, gets the same answer.
    # A break-glass account goes on to its password, from anywhere (#4532).
    if sso.required():
        matches = matching_accounts(session, payload.username_or_email)
        machine = len(matches) == 1 and _is_service_account(matches[0])
        marked = len(matches) == 1 and matches[0].break_glass_at is not None
        if not marked and (not machine or _is_browser_request(request)):
            if len(matches) == 1:
                refusal["principal"] = matches[0].principal_id
            failed("sso_required")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=sso.REQUIRED_DETAIL
            )

    # Authenticate user
    try:
        user = AuthService.authenticate_user(
            payload.username_or_email,
            payload.password,
            session,
            refusal=refusal,
            source=source,
        )
    except AccountLockedError:
        failed()
        raise
    except key_ring.KeyRingError:
        # Not a wrong password: nothing counted, the person may try again.
        logger.exception("Sign-in refused: the key ring cannot be read")
        failed("error")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Sign-in is unavailable right now. Try again shortly.",
        )

    if not user:
        failed()
        if refusal.get("reason") == PASSWORD_RESET_REQUIRED:
            # 403, not 401: no refresh-and-retry; nothing was counted.
            by_mail = mail_is_configured()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": PASSWORD_RESET_REQUIRED,
                    "message": "Your password must be reset before you can sign in. "
                    + (
                        "Use Forgot password to have a reset link mailed."
                        if by_mail
                        else "Mail is not set up on this server, so a reset link "
                        "cannot be mailed: ask an administrator."
                    ),
                    "by_mail": by_mail,
                },
            )
        raise _invalid_credentials()

    service_account = _is_service_account(user)
    # The host never marks a service account; the check keeps it that way here.
    marked = user.break_glass_at is not None and not service_account
    if service_account and _is_browser_request(request):
        # Exactly a wrong password, so the console can't tell the account apart.
        # Not a login either: drop authenticate_user's pending last_login /
        # login_count / lockout reset (the 401 also rolls the request back).
        if user in session:
            session.expire(user)
        logger.warning("Refused console sign-in for service account %s", user.username)
        failed("service_account_console")
        raise _invalid_credentials()

    new_recovery_codes = None
    recovered = False
    # Set when security keys are required of this person: how their sign-in got in
    # while keys are unavailable (a step-down), or that the app alone did not count.
    step_down: Optional[str] = None
    app_not_counted = False
    # No flush: authenticate_user's pending lockout reset would lock the row the
    # failed-code count below must update from its own transaction.
    with session.no_autoflush:
        held = AuthService.get_user_permissions(user.user_id, session)
    key_required = session_policy.mfa_covered(
        [name for name, on in held.items() if on],
        service_account,
        session_policy.policy().require_phishing_resistant,
    )
    # Whether the account had MFA when the password was given: a recovery below
    # clears it, and the length rule must still be the one the account signed in under.
    had_mfa = user.mfa_enabled
    reason = (payload.reason or "").strip()
    # Check MFA if enabled (a method of any kind: app, recovery codes or a key).
    # A break-glass account always reaches it, even with two-step turned off.
    if user.mfa_enabled or marked:
        if not payload.mfa_code and not payload.webauthn:
            return _mfa_required(session, user, key_required, marked)
        if marked and not reason:
            # Before the code is checked, so nothing is spent or counted.
            if user in session:
                session.expire(user)
            failed("break_glass_no_reason")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Give a reason for using this break-glass account.",
            )

        # authenticate_user clears the lockout counter on this session before
        # the code is checked. verify_mfa_code opens a savepoint, and
        # SQLAlchemy flushes pending changes before that SAVEPOINT, so the
        # cleared counter would lock the row until this request ends and the
        # failure count on the other connection would wait out the request.
        # Drop the pending reset first. Put it back only if the code is good.
        pending_reset = user in session
        factor = _second_factor(
            session,
            user,
            payload.mfa_code,
            payload.webauthn,
            payload.webauthn_challenge,
            key_required,
            source,
            failed,
            marked=marked,
            reason=reason,
        )
        step_down, app_not_counted = factor.step_down, factor.app_not_counted
        recovered, new_recovery_codes = factor.recovered, factor.new_codes
        if pending_reset:
            clear_lock(user)
            user.last_login = utcnow()
            user.login_count = (user.login_count or 0) + 1

    # The password and any MFA code are good; the password may have joined the
    # breached list since it was set, be too short for one factor, or be past the
    # maximum age. No tokens, cookies or sign-in row: a reset
    # token takes the person to /password-reset/confirm, the one forced-change
    # path. Returned, not raised, so a spent MFA code stays spent. A service
    # account's password is generated and it cannot reset.
    if not service_account:
        try:
            check_not_listed(payload.password, user_inputs=[user.username, user.email])
            if not marked:  # a break-glass password never expires
                check_not_expired(user)
            short_for_one_factor = min(
                SINGLE_FACTOR_MIN, session_policy.policy().password_min_length
            )
            if not had_mfa and len(payload.password) < short_for_one_factor:
                raise PasswordPolicyError(
                    f"Password is under {short_for_one_factor} characters, too short "
                    "without a second factor"
                )
        except PasswordPolicyError as exc:
            logger.info("Password change required at sign-in: %s", user.username)
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={
                    "detail": {
                        "code": PASSWORD_CHANGE_REQUIRED,
                        "message": f"{exc}. Choose a new password to sign in.",
                        "reset_token": invites.issue_reset(
                            session, user, invites.SIGNIN, user.principal_id
                        )[1],
                    }
                },
            )

    # Generate tokens. One sign-in, named on both, kept through every refresh.
    _ua = request.headers.get("user-agent")
    sid = sign_ins.new_id()
    limit = _limits(user, session)
    access_token = AuthService.generate_jwt_token(user, "access", sid=sid)
    refresh_token = AuthService.generate_jwt_token(
        user,
        "refresh",
        sid=sid,
        generation=0,
        not_after=utcnow() + limit.absolute,
    )

    refresh_payload = AuthService.verify_jwt_token(refresh_token) or {}
    # Past the account's sign-in limit, the oldest open sign-in ends (a service
    # account has none).
    evicted = sign_ins.make_room(session, user.principal_id, limit.sign_in_limit)
    auth_method = "mfa" if user.mfa_enabled and not app_not_counted else "password"
    if step_down:
        auth_method = (
            sign_ins.MFA_STEP_DOWN
            if step_down == "app"
            else sign_ins.RECOVERY_STEP_DOWN
        )
    if marked:
        auth_method = sign_ins.BREAK_GLASS
    # Under Require MFA off, this password was just checked, so a person with
    # no factor is not asked again for the confirmation window.
    password_confirmed = None
    if (
        not service_account
        and not user.mfa_enabled
        and not marked
        and session_policy.policy().require_mfa == "off"
    ):
        password_confirmed = utcnow()
    sign_ins.start(
        session,
        sid,
        user.principal_id,
        _exp_datetime(refresh_payload.get("exp")),
        auth_method=auth_method,
        user_agent=_ua,
        address=source,
        confirmed_at=password_confirmed,
    )
    if evicted:
        _tell_of_eviction(session, user, sid, _ua, source)
    trail_events.record(
        session,
        "auth.sign_in",
        {"source_ip": source},
        actor=user.principal_id,
        sign_in=sid,
        method=auth_method,
        **({"step_down": step_down} if step_down else {}),
    )
    # A service account gets body tokens only: a cookie is a console session.
    if not service_account:
        _set_cookies_for(response, access_token, refresh_token)

    logger.info(f"User logged in: {user.username}")

    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=_user_payload(user, session),
        recovery_codes=new_recovery_codes,
        mfa_removed=recovered,
    )


def _set_cookies_for(response: Response, access_token: str, refresh_token: str):
    # Extract exp claims so the cookie Max-Age matches the JWT lifetime.
    access_payload = AuthService.verify_jwt_token(access_token) or {}
    refresh_payload = AuthService.verify_jwt_token(refresh_token) or {}
    set_auth_cookies(
        response,
        access_token,
        refresh_token,
        access_exp=access_payload.get("exp"),
        refresh_exp=refresh_payload.get("exp"),
    )


def _exp_datetime(exp_ts) -> Optional[datetime]:
    return datetime.utcfromtimestamp(exp_ts) if exp_ts is not None else None


# --- single sign-on (#4284) ----------------------------------------------------


def _sso_cookie_path() -> str:
    return f"{context_path_prefix()}/api/auth/sso/"


def _sso_redirect(location: str) -> RedirectResponse:
    """A 302 that also clears the attempt's cookie: it is good for one callback."""
    res = RedirectResponse(location, status_code=status.HTTP_302_FOUND)
    res.delete_cookie(
        sso.COOKIE,
        path=_sso_cookie_path(),
        secure=_cookie_secure(),
        httponly=True,
        samesite="lax",
    )
    return res


def _sso_failed(
    source: Optional[str],
    refusal: sso.Refused,
    attempted: str = "",
    back: Optional[str] = None,
):
    """The login page with one of three fixed messages; the real reason goes to the trail.
    ``back``: the sign-in's return path, kept so a retry still lands there (#4789)."""
    trail_events.sign_in_failed(
        source, attempted, f"sso:{refusal.reason}", refusal.principal
    )
    logger.info("Single sign-on refused: %s", refusal.reason)
    query = {"sso_error": refusal.code}
    if back:
        query["return"] = _safe_return(back)
    return _sso_redirect(f"{context_path_prefix()}/login?{urlencode(query)}")


def _safe_return(path: Optional[str]) -> str:
    """A same-origin path to come back to, else the console's root: never an open redirect."""
    if (
        path
        and path.startswith("/")
        and not path.startswith("//")
        and "\\" not in path
        and path.isprintable()
    ):
        return path
    return f"{context_path_prefix()}/"


def _with_outcome(path: str, outcome: str, method: bool = False, **extra) -> str:
    # A method change comes back under its own mark, so it never reads as Confirm it's you.
    query = urlencode({"sso_method" if method else "sso_confirm": outcome, **extra})
    return f"{path}{'&' if '?' in path else '?'}{query}"


def _confirming(request: Request, method: bool = False) -> Optional[dict]:
    """The open sign-in an access cookie names, for Confirm it's you through the
    provider, or (``method``) for a method change in place of the password (#4756)."""
    raw = request.cookies.get(ACCESS_COOKIE_NAME)
    payload = AuthService.verify_jwt_token(raw) if raw else None
    if not payload or payload.get("token_type") != "access" or not payload.get("sid"):
        return None
    # A method change is a setup route: a person held at setup still gets there.
    user, refusal = _check_sign_in(
        payload.get("user_id"),
        payload["sid"],
        request.headers.get("user-agent"),
        False,
        method,
    )
    if user is None or refusal:
        return None
    # Only for whom the refusal (or the method dialog) offers it: anyone else
    # confirms the Vigil way.
    with unit_of_work() as session:
        if method:
            allowed = sso.sso_only(session, user)
        else:
            allowed = step_up.at_provider(session, user.principal_id, payload["sid"])
        if not allowed:
            return None
    return {
        "sid": payload["sid"],
        "principal": user.principal_id,
        "started": int(datetime.now(timezone.utc).timestamp()),
        **({"method": True} if method else {}),
    }


@router.get("/sso/start", include_in_schema=False)
@limiter.limit("10/minute", key_func=sso_rate_key)
def sso_start(
    request: Request,
    confirm: Optional[str] = None,
    method: Optional[str] = None,
    next: Optional[str] = None,
):
    """Send the browser to the provider: code flow with PKCE, state and nonce,
    bound to this browser by a signed cookie that lives five minutes.

    ``confirm``: the path to come back to after Confirm it's you at the provider
    (#4285), for the sign-in this browser already holds. With ``method``, it is for
    a method change instead: the return carries a proof in place of the password
    (#4756). ``next``: for a sign-in, the console path to land on afterwards,
    such as an app's consent page (#4789)."""
    held = None
    if confirm is not None:
        changing = bool(method)
        held = _confirming(request, changing)
        if held is None:
            # Back with "failed": the console's next request refreshes or signs out as it should.
            return _sso_redirect(
                _with_outcome(_safe_return(confirm), "failed", changing)
            )
        held["return"] = _safe_return(confirm)
    try:
        # A normal sign-in asks the provider to repeat one older than the
        # sign-in length: a claim counts only with an auth_time inside it.
        max_age = None if held else int(_sso_absolute().total_seconds())
        back = _safe_return(next) if next and held is None else None
        url, binding = sso.begin(sso.provider(), held, max_age, back)
    except (sso.Refused, key_ring.KeyRingError) as exc:
        if held is not None:
            logger.info("Confirm it's you through SSO did not start: %s", exc)
            return _sso_redirect(
                _with_outcome(held["return"], "failed", bool(held.get("method")))
            )
        refusal = (
            exc if isinstance(exc, sso.Refused) else sso.Refused("key_ring_unavailable")
        )
        return _sso_failed(client_host(request), refusal)
    return _with_sso_cookie(RedirectResponse(url, status.HTTP_302_FOUND), binding)


def _with_sso_cookie(res: Response, value: str) -> Response:
    # Lax, not the auth cookies' setting: it has to ride the provider's top-level redirect back.
    res.set_cookie(
        sso.COOKIE,
        value,
        max_age=int(sso.LIFE.total_seconds()),
        path=_sso_cookie_path(),
        secure=_cookie_secure(),
        httponly=True,
        samesite="lax",
    )
    return res


def _sso_absolute() -> timedelta:
    """The absolute sign-in length: how recent a provider's MFA must be to count."""
    return timedelta(hours=session_policy.policy().absolute_hours)


@router.get("/sso/callback", include_in_schema=False)
@limiter.limit("10/minute", key_func=sso_rate_key)
def sso_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    """Where the provider sends the browser back: check the reply and the ID
    token, find or create the account, and sign it in as a password would."""
    source = client_host(request)
    seen: dict = {}
    held: dict = {}
    try:
        held = sso.binding(request.cookies.get(sso.COOKIE), state)
        if isinstance(held.get("confirm"), dict):
            return _sso_confirm(held, code, error, source)
        if error or not code:
            raise sso.Refused("provider_error" if error else "no_code")
        p = sso.provider()
        claims = sso.verify(p, sso.exchange(p, code, held["verifier"]), held["nonce"])
        seen["email"] = claims["email"]
        with unit_of_work() as session:
            arrived = _sso_arrive(session, request, p.settings, claims, source)
    except sso.Refused as refusal:
        return _sso_failed(
            source,
            refusal,
            refusal.attempted or seen.get("email", ""),
            held.get("return"),
        )
    except (
        Exception
    ):  # noqa: BLE001 - e.g. a concurrent first sign-in's link: never a 500
        logger.exception("Single sign-on callback failed")
        return _sso_failed(
            source, sso.Refused("unexpected"), seen.get("email", ""), held.get("return")
        )
    back = _safe_return(held.get("return"))
    if isinstance(arrived, str):
        # The login page asks for the Vigil factor; the waiting sign-in takes
        # the state cookie's place. The page lands on ``return`` afterwards.
        step = {"sso_step": 1}
        if held.get("return"):
            step["return"] = back
        return _with_sso_cookie(
            RedirectResponse(
                f"{context_path_prefix()}/login?{urlencode(step)}",
                status.HTTP_302_FOUND,
            ),
            arrived,
        )
    res = _sso_redirect(back)
    _set_cookies_for(res, *arrived[:2])
    return res


def _sso_confirm(held: dict, code, error, source: Optional[str]) -> RedirectResponse:
    """Confirm it's you at the provider (#4285, epic decision 2): the same subject,
    signed in again just now, reopens the window on the sign-in already open. No
    sign-in, cookie, account, link or role is touched."""
    c = held["confirm"]
    back, sid, pid = _safe_return(c.get("return")), c["sid"], c["principal"]
    changing = bool(c.get("method"))
    try:
        if error or not code:
            raise sso.Refused("provider_error" if error else "no_code")
        p = sso.provider()
        claims = sso.verify(p, sso.exchange(p, code, held["verifier"]), held["nonce"])
        with unit_of_work() as session:
            link = sso.linked(session, p.settings.issuer, claims["sub"])
    except sso.Refused as refusal:
        # Nothing was answered (a cancel, a provider fault): not a failed confirmation.
        logger.info("Confirm it's you through SSO refused: %s", refusal.reason)
        return _sso_redirect(_with_outcome(back, "failed", changing))
    auth_time = claims.get("auth_time")
    problem = None
    if link is None or link.principal_id != pid:
        problem = "subject_mismatch"
    elif (
        not isinstance(auth_time, (int, float))
        or isinstance(auth_time, bool)
        or auth_time < c["started"] - sso.LEEWAY
    ):
        problem = "auth_time_stale"
    if problem:
        logger.info("Confirm it's you through SSO failed: %s", problem)
        # The third in a row ends the sign-in; the console's next request shows it.
        step_up._fail(sid, pid, source)
        return _sso_redirect(_with_outcome(back, "failed", changing))
    if changing:
        # A proof for the method change, never the window: a person who holds a
        # Vigil factor must still use it (epic #4751 decisions 7 and 10).
        with unit_of_work() as session:
            user = session.query(User).filter(User.principal_id == pid).first()
            row = session.get(SignIn, sid)
            still = (
                user is not None
                and row is not None
                and row.ended_at is None
                and row.principal_id == pid
                and sso.sso_only(session, user)
            )
        if not still:
            return _sso_redirect(_with_outcome(back, "failed", True))
        proof = sso.issue_proof(sid, pid)
        return _sso_redirect(_with_outcome(back, "ok", True, sso_proof=proof))
    if (
        sso.judged(claims, _sso_absolute())[0] is None
        and session_policy.policy().require_mfa != "off"
    ):
        # Not a failed confirmation: the provider confirmed who, not a second factor.
        # Require MFA off does not assume a Vigil factor, so the round-trip below
        # opens the window. Any other setting still sends them to set one up.
        logger.info("Confirm it's you through SSO: no qualifying MFA claim")
        return _sso_redirect(_with_outcome(back, step_up.MFA_FIRST))
    with unit_of_work() as session:
        # Still the person who confirms at the provider, with the sign-in still open.
        # Under off, that person has no Vigil factor to set up, so a password
        # sign-in left open after the password was removed counts too. A sign-in
        # Require SSO would now refuse ends instead, and opens nothing.
        user = session.query(User).filter(User.principal_id == pid).first()
        row = session.get(SignIn, sid)
        open_row = (
            user is not None
            and row is not None
            and row.ended_at is None
            and row.principal_id == pid
        )
        if open_row and step_up.non_sso_now_refused(user, row.auth_method):
            sign_ins.end(sid, sign_ins.SSO_REQUIRED, session)
            done = False
        else:
            holds = step_up.at_provider(session, pid, sid) or (
                open_row
                and session_policy.policy().require_mfa == "off"
                and step_up.provider_only(session, user)
            )
            done = bool(holds) and step_up.confirmed(
                session, pid, sid, source, factor=step_up.PROVIDER
            )
    return _sso_redirect(_with_outcome(back, "ok" if done else "failed"))


def _sso_username(session: Session, claims: dict) -> str:
    """A free username from the token: preferred_username, else the email's local part."""
    wanted = claims.get("preferred_username")
    if not isinstance(wanted, str) or "@" in wanted:
        wanted = claims["email"].split("@", 1)[0]
    base = re.sub(r"[^A-Za-z0-9._-]", "", wanted)[:80] or "user"
    if base.lower().startswith(("medic-", ERASED_PREFIX)):
        base = f"sso-{base}"
    for n in range(1, 1000):
        name = base if n == 1 else f"{base}-{n}"
        claim_sign_in_names(session, name)
        if not identifier_in_use(session, name):
            return name
    raise sso.Refused("no_free_username")


_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_LOWER = _UPPER.lower()
_ASCII_LOWER = str.maketrans(_UPPER, _LOWER)


def _sso_account(session: Session, s, claims: dict, notes: dict) -> Optional[User]:
    """The account this subject signs in as (None: create one), refused when it
    can't. A directory id on an unmanaged link is refreshed here; a refusal
    rolls that back with the sign-in."""
    email = claims["email"].strip()
    claim = directory_sync.claim_value(claims, s.directory_id_claim)
    if claim:
        notes["directory_claim"] = claim
    link = sso.linked(session, s.issuer, claims["sub"])
    if link is not None:
        if claim:
            directory_sync.note_directory_id(session, link, s.issuer, claim)
        notes["linked_by"] = "subject"
    elif claim:
        # Sync on: this is how a push is found. Sync off: a placeholder subject
        # (still the externalId) is still replaced, so turning sync off does not
        # strand that person on the email match.
        link = sso.linked_by_directory(session, s.issuer, claim)
        # Only a link still waiting for its first sign-in: once a subject is
        # bound, that subject alone reaches the account.
        if link is not None and link.subject != link.directory_id:
            link = None
        if link is not None:
            directory_sync.adopt_subject(session, link, s.issuer, claims["sub"])
            notes["linked_by"] = "directory"
    if link is not None:
        if link.managed:
            notes["managed"] = True
        user = (
            session.query(User)
            .filter(User.principal_id == link.principal_id)
            .with_for_update()
            .first()
        )
    elif s.sync_enabled:
        raise sso.Refused("not_provisioned", sso.NOT_PROVISIONED, attempted=email)
    else:
        claim_sign_in_names(session, email)
        matches = (
            session.query(User)
            # ASCII case only, on both sides: Unicode case mapping folds other
            # mailboxes onto this one (U+212A KELVIN SIGN lowers to "k").
            .filter(
                func.translate(User.email, _UPPER, _LOWER)
                == email.translate(_ASCII_LOWER)
            )
            .with_for_update()
            .all()
        )
        if len(matches) > 1:
            raise sso.Refused("email_ambiguous")
        user = matches[0] if matches else None
        if user is None:
            # Signing in here answers a pending invite for the address.
            invites.revoke_for_sso(session, email)
            taken = identifier_in_use(session, email)
            if taken in (RESERVED, UNAVAILABLE):
                # The trail names the provider's subject, never the address.
                raise sso.Refused(
                    "email_reserved", attempted=f"{s.issuer}#{claims['sub']}"
                )
            if taken:
                raise sso.Refused("email_in_use")
            notes["linked_by"] = "created"
            return None
        # Taking over an existing account on its email needs the provider to
        # vouch for that address; a missing claim is not a yes. Some providers
        # send the claim as a string.
        if claims.get("email_verified") not in (True, "true"):
            raise sso.Refused(
                "email_link_unverified",
                sso.EMAIL_NOT_VERIFIED,
                principal=user.principal_id,
                attempted=email,
            )
        notes["linked_by"] = "email"
    if user is None:
        raise sso.Refused("linked_account_missing")
    principal = session.get(Principal, user.principal_id)
    pid = user.principal_id
    if is_erased(user.username, pid):
        raise sso.Refused("account_erased", principal=pid)
    if principal is None or principal.deleted_at is not None:
        raise sso.Refused("account_deleted", principal=pid)
    if _is_service_account(user) or principal.kind != principals.PERSON:
        raise sso.Refused("service_account", principal=pid)
    if not user.is_active:
        raise sso.Refused("account_deactivated", sso.DEACTIVATED, pid)
    if user.break_glass_at is not None:
        # Never linked: found by email, or by a link made before the mark.
        code = sso.BREAK_GLASS_EMAIL if link is None else sso.BREAK_GLASS_LINKED
        raise sso.Refused("break_glass_account", code, principal=pid, attempted=email)
    if link is None:
        other = session.query(SsoLink).filter(
            SsoLink.principal_id == pid, SsoLink.issuer == s.issuer
        )
        if other.first() is not None:
            # Linked already to another subject at this provider: not taken by email.
            raise sso.Refused("linked_to_other_subject", principal=pid)
    return user


def _sso_role(s, claims: dict, notes: dict) -> Optional[tuple[str, str]]:
    """(role, group) of the first mapping whose group the token carries, exact strings."""
    groups = sso.groups_of(claims, s.groups_claim)
    if groups is None:
        notes["groups"] = "claim_missing"
        return None
    for mapping in s.group_roles:
        if mapping.group in groups:
            return mapping.role_id, mapping.group
    notes["groups"] = "no_match"
    return None


# What a waiting sign-in keeps of the token: enough to finish, never the groups.
_SSO_KEPT = (
    "sub", "email", "email_verified", "name", "given_name", "family_name",
    "preferred_username", "amr", "acr", "auth_time",
)  # fmt: skip


def _sso_key_required(session: Session, user: User, match, notes: dict) -> bool:
    """Whether Require phishing-resistant MFA covers the role this sign-in carries."""
    role_id = match[0] if match and not notes.get("managed") else user.role_id
    role = session.get(Role, role_id) or session.get(Role, user.role_id)
    held = [name for name, on in ((role and role.permissions) or {}).items() if on]
    return session_policy.mfa_covered(
        held, False, session_policy.policy().require_phishing_resistant
    )


def _sso_met(
    session: Session, user: User, key_required: bool, met: Optional[str]
) -> Optional[str]:
    """How the provider's word meets MFA for this person (``sign_ins`` value), or
    None: ask for their Vigil factor (epic #4751 decision 6). A person with no
    factor goes on, and ``setup_needed`` holds them if they are covered."""
    if met == sign_ins.PROVIDER_PHR:
        return met
    if met == sign_ins.PROVIDER_MFA:
        if not key_required:
            return met
        if not security_keys.availability().available:
            return sign_ins.STEP_DOWN  # the provider's mfa stands in, as the app does
        # Keys required and working: a key holder is asked for it; anyone else
        # goes on and is held at "Add a security key".
        return None if security_keys.has_keys(session, user.principal_id) else met
    return None if user.mfa_enabled else sign_ins.NONE


def _sso_arrive(
    session: Session, request: Request, s, claims: dict, source: Optional[str]
) -> Union[tuple[str, str, User, str], str]:
    """The callback's half: find the account and judge the token. Signed in
    (the tokens) when the provider's word is enough or there is no Vigil factor
    to ask for; otherwise the waiting sign-in's cookie, with everything this
    transaction did rolled back, so nothing is written before the factor."""
    notes: dict = {}
    user = _sso_account(session, s, claims, notes)
    match = _sso_role(s, claims, notes)
    met, auth_time = sso.judged(claims, _sso_absolute())
    # A new account has no factor to ask for: created, then held if covered.
    key_required = user is not None and _sso_key_required(session, user, match, notes)
    how = _sso_met(session, user, key_required, met) if user else met or sign_ins.NONE
    if how is None:
        kept = {k: claims[k] for k in _SSO_KEPT if k in claims}
        if notes.get("directory_claim"):
            kept[s.directory_id_claim] = notes["directory_claim"]
        waiting = {
            "principal": user.principal_id,
            "claims": kept,
            "match": list(match) if match else None,
            "groups": notes.get("groups"),
            "met": met,
            "key_required": key_required,
        }
        session.rollback()
        return sso.hold(waiting)
    provider = how != sign_ins.NONE and met is not None
    return _sso_sign_in(
        session,
        request,
        s,
        claims,
        source,
        user,
        match,
        notes,
        how,
        auth_time if provider else None,
    )


def _sso_sign_in(
    session: Session,
    request: Request,
    s,
    claims: dict,
    source: Optional[str],
    user: Optional[User],
    match: Optional[tuple[str, str]],
    notes: dict,
    mfa_met: str,
    provider_time: Optional[int] = None,
) -> tuple[str, str, User, str]:
    """Link or create the account ``_sso_account`` found, apply name, email and
    role from the token, and start the sign-in, recorded with how it met MFA.
    ``provider_time``: the provider's auth_time when its MFA counted, which opens
    the window and brings the end forward. Returns the access and refresh tokens,
    the account and the sign-in id."""
    from services.api.routers.users import _leaves_no_manager

    email = claims["email"].strip()
    given = " ".join(
        str(claims[k]) for k in ("given_name", "family_name") if claims.get(k)
    )
    name = claims.get("name") if isinstance(claims.get("name"), str) else given
    name = (name or "").strip()[:200] or None  # users.full_name

    if user is None:
        role_id = match[0] if match else s.default_role_id
        if session.get(Role, role_id) is None:
            raise sso.Refused("role_missing")
        username = _sso_username(session, claims)
        user = User(
            user_id=f"user-{uuid.uuid4().hex[:12]}",
            username=username,
            email=email,
            password_hash="",
            full_name=name or username,
            role_id=role_id,
            is_active=True,
            is_verified=True,
            mfa_enabled=False,
            login_count=0,
        )
        principals.add_for_user(session, user)
        session.add(user)
        session.flush()
        join_default(session, user.principal_id, actor=SYSTEM)
        trail_events.record(
            session,
            "user.created",
            {"personal": trail_events.personal(user)},
            actor=SYSTEM,
            principal=user.principal_id,
            role_id=role_id,
            via="sso",
            provider=s.issuer,
            group=match[1] if match else None,
        )
    elif not notes.get("managed"):
        changed = []
        if name and name != user.full_name:
            user.full_name = name
            changed.append("full_name")
        if email != user.email:
            claim_sign_in_names(session, email)
            taken = identifier_in_use(session, email, except_user_id=user.user_id)
            if taken in (RESERVED, UNAVAILABLE):
                notes["email_reserved"] = True
            elif taken:
                notes["email_conflict"] = True
            else:
                user.email = email
                user.is_verified = True
                invites.revoke_confirms(session, user.principal_id, SYSTEM, "sso")
                changed.append("email")
        if changed:
            trail_events.record(
                session,
                "user.updated",
                {"personal": {k: getattr(user, k) for k in changed}},
                actor=SYSTEM,
                principal=user.principal_id,
                changed=changed,
                via="sso",
            )
        if match and match[0] != user.role_id:
            role = session.get(Role, match[0])
            if role is None:
                notes["role_kept"] = "role_missing"
            elif _leaves_no_manager(
                session, users={user.user_id: (role.role_id, True)}
            ):
                notes["role_kept"] = "last_role_manager"
            else:
                previous = session.get(Role, user.role_id)
                before = sorted(
                    k
                    for k, v in ((previous and previous.permissions) or {}).items()
                    if v
                )
                trail_events.record(
                    session,
                    "user.role_changed",
                    actor=SYSTEM,
                    principal=user.principal_id,
                    role_id=role.role_id,
                    previous_role_id=user.role_id,
                    permissions_before=before,
                    permissions_after=sorted(
                        k for k, v in (role.permissions or {}).items() if v
                    ),
                    via="sso",
                    group=match[1],
                )
                user.role_id = role.role_id
                # The new sign-in starts after this, so every open one ends.
                _end_others_after_change(session, user, None, source)
    if notes.get("linked_by") not in ("subject", "directory"):
        created = sso.link(session, user.principal_id, s.issuer, claims["sub"])
        claim = notes.get("directory_claim")
        if isinstance(claim, str):
            directory_sync.note_directory_id(session, created, s.issuer, claim)
    session.flush()

    ua = request.headers.get("user-agent")
    sid = sign_ins.new_id()
    limit = _limits(user, session)
    now = utcnow()
    # A provider's MFA lasts the sign-in length from when it was given (decision 2).
    factor_at = _exp_datetime(provider_time) if provider_time is not None else None
    ends_at = min(now, factor_at) + limit.absolute if factor_at else None
    access_token = AuthService.generate_jwt_token(user, "access", sid=sid)
    refresh_token = AuthService.generate_jwt_token(
        user,
        "refresh",
        sid=sid,
        generation=0,
        not_after=ends_at or now + limit.absolute,
    )
    refresh_payload = AuthService.verify_jwt_token(refresh_token) or {}
    evicted = sign_ins.make_room(session, user.principal_id, limit.sign_in_limit)
    sign_ins.start(
        session,
        sid,
        user.principal_id,
        _exp_datetime(refresh_payload.get("exp")),
        auth_method=sign_ins.SSO,
        user_agent=ua,
        address=source,
        mfa_met=mfa_met,
        confirmed_at=factor_at,
        ends_at=ends_at,
    )
    if evicted:
        _tell_of_eviction(session, user, sid, ua, source)
    user.last_login = utcnow()
    user.login_count = (user.login_count or 0) + 1
    trail_events.record(
        session,
        "auth.sign_in",
        {"source_ip": source},
        actor=user.principal_id,
        sign_in=sid,
        method=sign_ins.SSO,
        provider=s.issuer,
        amr=claims.get("amr"),
        acr=claims.get("acr"),
        mfa_met=mfa_met,
        auth_time=claims.get("auth_time"),
        **notes,
    )
    logger.info("User signed in through single sign-on: %s", user.username)
    return access_token, refresh_token, user, sid


class SsoFinishRequest(BaseModel):
    """The Vigil factor after the provider: a code, or a key's assertion and the
    challenge the first call's 401 carried."""

    mfa_code: Optional[str] = None
    webauthn: Optional[dict] = None
    webauthn_challenge: Optional[str] = None


def _sso_refused(source: Optional[str], refusal: sso.Refused, attempted: str):
    """A waiting sign-in that can't finish: the login page's SSO message by code.
    Raised, so a code spent on the way is not."""
    trail_events.sign_in_failed(
        source, attempted, f"sso:{refusal.reason}", refusal.principal
    )
    logger.info("Single sign-on refused at the second step: %s", refusal.reason)
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "sso_refused", "sso_error": refusal.code},
    )


@router.post("/sso/finish", response_model=LoginResponse)
@limiter.limit("5/minute", key_func=sso_rate_key)
def sso_finish(
    request: Request,
    response: Response,
    payload: SsoFinishRequest,
    session: UnitOfWorkSession,
):
    """The second step of a sign-in through the provider whose token didn't meet
    MFA (#4757): the person's Vigil factor, checked exactly as ``login`` checks
    it. Without one, the 401 ``X-MFA-Required`` offer, as login's first call.
    Only once it holds is the account found, updated and signed in."""
    source = client_host(request)
    held = sso.waiting(request.cookies.get(sso.COOKIE))
    if held is None:
        raise _sso_refused(source, sso.Refused("pending_expired", "expired"), "")
    claims, pid = held["claims"], held["principal"]
    email = claims.get("email", "")

    def failed(reason: str) -> None:
        trail_events.sign_in_failed(source, email, reason, pid)

    s = sso_settings.settings()
    user = session.query(User).filter(User.principal_id == pid).first()
    if not sso.is_on(s):
        raise _sso_refused(source, sso.Refused("sso_off", principal=pid), email)
    if user is None or not user.is_active:
        raise _sso_refused(
            source, sso.Refused("account_deactivated", sso.DEACTIVATED, pid), email
        )
    if _account_is_locked(user):
        failed("locked")
        raise AccountLockedError(user.locked_until)
    key_required = bool(held.get("key_required"))
    if not payload.mfa_code and not payload.webauthn:
        return _mfa_required(session, user, key_required)
    factor = _second_factor(
        session,
        user,
        payload.mfa_code,
        payload.webauthn,
        payload.webauthn_challenge,
        key_required,
        source,
        failed,
    )
    clear_lock(user)
    met = held.get("met")
    if factor.step_down:
        how, provider_time = sign_ins.STEP_DOWN, None
    elif user.mfa_enabled and not factor.app_not_counted:
        how, provider_time = sign_ins.VIGIL_FACTOR, None
    elif met:  # a recovery code took the methods; the provider's mfa still counts
        how, provider_time = met, claims.get("auth_time")
    else:
        how, provider_time = sign_ins.NONE, None
    match = tuple(held["match"]) if held.get("match") else None
    notes: dict = {"groups": held["groups"]} if held.get("groups") else {}
    if factor.step_down:  # on the trail as login puts it
        notes["step_down"] = factor.step_down
    try:
        found = _sso_account(session, s, claims, notes)
        if found is None or found.principal_id != pid:
            raise sso.Refused("account_changed", principal=pid)
        access, refresh, user, sid = _sso_sign_in(
            session,
            request,
            s,
            claims,
            source,
            found,
            match,
            notes,
            how,
            provider_time,
        )
    except sso.Refused as refusal:
        raise _sso_refused(source, refusal, refusal.attempted or email)
    _set_cookies_for(response, access, refresh)
    response.delete_cookie(
        sso.COOKIE,
        path=_sso_cookie_path(),
        secure=_cookie_secure(),
        httponly=True,
        samesite="lax",
    )
    return LoginResponse(
        access_token=access,
        refresh_token=refresh,
        user=_user_payload(user, session, sid),
        recovery_codes=factor.new_codes,
        mfa_removed=factor.recovered,
    )


@router.post("/logout")
@mfa_setup_route
async def logout(
    request: Request,
    response: Response,
    body: Optional[RefreshTokenRequest] = None,
    current_user: User = Depends(get_current_active_user),
    authorization: Optional[str] = Header(None),
):
    """
    Logout user: end the sign-in the tokens name, so replaying either token
    returns 401, and clear the HttpOnly auth cookies.

    Args:
        request: FastAPI request (used to read the auth cookies).
        response: FastAPI response (used to clear auth cookies).
        body: Optional refresh token for Bearer-flow clients, which hold it
            outside a cookie.
        current_user: Current authenticated user.
        authorization: Authorization header (used to extract the sign-in for
            Bearer-flow clients).

    Returns:
        Success message.
    """
    # Prefer the cookie (browser flow). Fall back to Bearer for API clients.
    raw_token: Optional[str] = request.cookies.get(ACCESS_COOKIE_NAME)
    if not raw_token and authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            raw_token = parts[1]
    # The cookie and the body field can differ (a Bearer client next to a
    # browser session), so every token presented names its sign-in. Another
    # user's token is left alone.
    sids = set()
    for raw in {
        raw_token,
        request.cookies.get(REFRESH_COOKIE_NAME),
        body.refresh_token if body else None,
    } - {None, ""}:
        payload = AuthService.verify_jwt_token(raw)
        if payload and payload.get("user_id") == current_user.user_id:
            sids.add(payload.get("sid"))

    # A safety action: recorded, or held to be recorded late. Only a sign-in
    # this call actually ended is recorded.
    for sid in sids - {None}:
        if await asyncio.to_thread(sign_ins.end, sid, sign_ins.SIGN_OUT):
            await asyncio.to_thread(
                trail_events.safety,
                "auth.sign_out",
                {"source_ip": client_host(request)},
                actor=current_user.principal_id,
                sign_in=sid,
            )

    clear_auth_cookies(response)
    # The browser drops what it holds for this origin (the cookies are cleared above).
    response.headers["Clear-Site-Data"] = '"cache", "storage"'

    logger.info(f"User logged out: {current_user.username}")
    return {"message": "Logged out successfully"}


class ConfirmRequest(BaseModel):
    """The one-factor check before ending sign-ins: the password for a person
    without MFA, a current authenticator or recovery code for one with it."""

    password: Optional[str] = None
    code: Optional[str] = None
    assertion: Optional[dict] = None
    challenge: Optional[str] = None


def _confirm_it_is_you(
    request: Request, user: User, body: ConfirmRequest, session: Session
) -> None:
    """A fresh one-factor check on every call. It shares the confirmation's
    window and its count of failures in a row.

    With a trail, the window opens only in the transaction that appends
    ``auth.confirmed``. A refused insert is held and the window stays shut,
    so the check grants nothing further; the sign-ins still end.
    """
    sid = _session_sid(request)
    source = client_host(request)
    if not trail_events.ready(session):
        step_up.confirm(
            user,
            session,
            sid,
            password=body.password,
            code=body.code,
            assertion=body.assertion,
            challenge=body.challenge,
            source=source,
        )
        return
    factor = step_up.confirm(
        user,
        session,
        sid,
        password=body.password,
        code=body.code,
        assertion=body.assertion,
        challenge=body.challenge,
        source=source,
        settle=False,
    )
    _settle_end_confirmation(
        user, sid, source, not step_up.provider_only(session, user), factor
    )


def _settle_end_confirmation(
    user: User,
    sid: Optional[str],
    source: Optional[str],
    window: bool = True,
    factor: Optional[str] = None,
) -> None:
    """Append ``auth.confirmed`` and open the window together, or hold it shut.
    ``window=False``: append it only (the password never opens the window of a
    person who confirms at the provider). ``factor`` names what held."""
    beside = {"source_ip": source}
    fields = dict(
        actor=user.principal_id,
        principal=user.principal_id,
        sign_in=sid,
        factor=factor,
    )
    try:
        with unit_of_work() as own:
            if window and sid and not step_up.open_window(own, sid):
                return
            trail_events.record(own, "auth.confirmed", beside, **fields)
        return
    except Exception:  # noqa: BLE001 - the sign-ins still end; the entry is held
        logger.exception("auth.confirmed was not written with the window")
    held = trail_events.safety("auth.confirmed", beside, **fields)
    if held is not None and window and sid:
        with unit_of_work() as own:
            step_up.open_window(own, sid)


class ConfirmItRequest(ConfirmRequest):
    """Confirm it's you for a privileged change. ``strong``: the password and,
    with MFA, a current code, whatever the window."""

    strong: bool = False


class ConfirmedResponse(BaseModel):
    """The window the confirmation opened."""

    confirmed_at: IsoDateTime
    window_ends_at: IsoDateTime


@router.post("/confirm", response_model=ConfirmedResponse)
@limiter.limit("10/minute", key_func=session_rate_key)
def confirm_it_is_you(
    request: Request,
    body: ConfirmItRequest,
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: UnitOfWorkSession,
):
    """Confirm it's you: opens the window the privileged changes read. A wrong
    answer is a 400; the third in a row ends the sign-in (401, never a lock)."""
    sid = _session_sid(request)
    # Before the provider-only answer: a password sign-in left open when Require
    # single sign-on was turned on ends here, for a person with or without MFA.
    step_up.end_non_sso(session, current_user, sid)
    # One who confirms at the provider opens the window only there, never with
    # the password.
    if step_up.provider_only(session, current_user):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, step_up.CONFIRM_AT_PROVIDER)
    step_up.confirm(
        current_user,
        session,
        sid,
        password=body.password,
        code=body.code,
        assertion=body.assertion,
        challenge=body.challenge,
        strong=body.strong,
        source=client_host(request),
    )
    now = utcnow()
    return ConfirmedResponse(confirmed_at=now, window_ends_at=now + step_up.window())


def _end_own(
    request: Request,
    response: Response,
    session: Session,
    user: User,
    ids: List[str],
) -> sign_ins.EndedSignIns:
    """End these sign-ins of the caller's.

    With a trail, the ends commit first and each entry is appended or held.
    Without one, both stay in the request transaction (rows locked before the
    trail's lock, the order every other path takes).
    """
    beside = {"source_ip": client_host(request)}
    fields = dict(
        actor=user.principal_id,
        principal=user.principal_id,
        end_reason=sign_ins.ENDED_BY_PERSON,
    )
    if trail_events.ready(session):
        with unit_of_work() as own:
            ended = [
                sid for sid in ids if sign_ins.end(sid, sign_ins.ENDED_BY_PERSON, own)
            ]
        for sid in ended:
            trail_events.safety("auth.sign_in_ended", beside, sign_in=sid, **fields)
    else:
        ended = [
            sid for sid in ids if sign_ins.end(sid, sign_ins.ENDED_BY_PERSON, session)
        ]
        for sid in ended:
            trail_events.record(
                session, "auth.sign_in_ended", beside, sign_in=sid, **fields
            )
    here = _session_sid(request) in ended
    if here:
        clear_auth_cookies(response)
    return sign_ins.EndedSignIns(ended=len(ended), signed_out=here)


@router.get("/sign-ins", response_model=List[sign_ins.SignInEntry])
def list_my_sign_ins(
    request: Request,
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: UnitOfWorkSession,
):
    """Your open sign-ins and recently ended ones; the one asking is marked."""
    return sign_ins.listing(session, current_user.principal_id, _session_sid(request))


@router.post("/sign-ins/end-others", response_model=sign_ins.EndedSignIns)
@limiter.limit("5/minute", key_func=session_rate_key)
def end_other_sign_ins(
    request: Request,
    response: Response,
    body: ConfirmRequest,
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: UnitOfWorkSession,
):
    """Sign out everywhere else: every open sign-in but this one."""
    _confirm_it_is_you(request, current_user, body, session)
    ids = [
        sid
        for sid, _ in sign_ins.open_ids(
            session, current_user.principal_id, keep=_session_sid(request)
        )
    ]
    return _end_own(request, response, session, current_user, ids)


@router.post("/sign-ins/end-all", response_model=sign_ins.EndedSignIns)
@limiter.limit("5/minute", key_func=session_rate_key)
def end_all_my_sign_ins(
    request: Request,
    response: Response,
    body: ConfirmRequest,
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: UnitOfWorkSession,
):
    """Sign out everywhere: every open sign-in, this one too."""
    _confirm_it_is_you(request, current_user, body, session)
    ids = [sid for sid, _ in sign_ins.open_ids(session, current_user.principal_id)]
    return _end_own(request, response, session, current_user, ids)


@router.post("/sign-ins/{sign_in_id}/end", response_model=sign_ins.EndedSignIns)
@limiter.limit("5/minute", key_func=session_rate_key)
def end_my_sign_in(
    request: Request,
    response: Response,
    sign_in_id: str,
    body: ConfirmRequest,
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: UnitOfWorkSession,
):
    """End one of your open sign-ins."""
    _confirm_it_is_you(request, current_user, body, session)
    mine = [sid for sid, _ in sign_ins.open_ids(session, current_user.principal_id)]
    if sign_in_id not in mine:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Sign-in not found"
        )
    return _end_own(request, response, session, current_user, [sign_in_id])


@router.post("/refresh", response_model=LoginResponse)
@limiter.limit("30/minute", key_func=refresh_rate_key)
async def refresh_token(
    request: Request,
    response: Response,
    body: Optional[RefreshTokenRequest] = None,
    *,
    session: UnitOfWorkSession,
):
    """
    Refresh access token using refresh token.

    Token source priority:
    1. `refresh_token` HttpOnly cookie (browser flow)
    2. `refresh_token` field in the request body (Bearer-flow API clients)

    Args:
        request: FastAPI request (used by the rate limiter and to read
            the refresh_token cookie).
        response: FastAPI response (used to set the new auth cookies).
        body: Optional refresh-token body for API clients.
        session: Database session.

    Returns:
        New access and refresh tokens.
    """
    raw_refresh: Optional[str] = request.cookies.get(REFRESH_COOKIE_NAME)
    from_cookie = bool(raw_refresh)
    if not raw_refresh and body is not None:
        raw_refresh = body.refresh_token
    if not raw_refresh:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token missing",
        )

    payload = AuthService.verify_jwt_token(raw_refresh)
    if not payload:
        # A sign-in whose refresh token ran out ends here, in its own
        # transaction: the request's rolls back with the 401.
        expired = AuthService.expired_refresh_payload(raw_refresh)
        if expired and expired.get("sid"):
            try:
                if await asyncio.to_thread(
                    sign_ins.end, expired["sid"], sign_ins.EXPIRY
                ):
                    return _ended(sign_ins.EXPIRY)
            except Exception as exc:
                logger.error("Failed to end an expired sign-in: %s", exc)
    if not payload or payload.get("token_type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_REFRESH_TOKEN,
        )

    sid, generation = payload.get("sid"), payload.get("gen")
    if not sid or generation is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_REFRESH_TOKEN,
        )

    user = await asyncio.to_thread(_get_user, session, payload["user_id"])
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    service_account = _is_service_account(user)
    if service_account and (from_cookie or _is_browser_request(request)):
        # A service account never held a cookie, and a console refresh is a
        # console session: answer as a bad token and clear what the browser has.
        logger.warning("Refused console refresh for service account %s", user.username)
        refused = JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": _INVALID_REFRESH_TOKEN},
        )
        clear_auth_cookies(refused)
        return refused

    # Everything that touches the sign-in row runs in one thread hop. The row
    # lock is held until the request commits, so no await may sit between
    # taking it and returning: a concurrent refresh of the same sign-in parks a
    # worker thread on that lock, and a holder that has to wait for a free
    # thread while they all wait on it never releases it.
    outcome = await asyncio.to_thread(
        _continue_sign_in,
        session,
        sid,
        generation,
        user,
        request.headers.get("user-agent"),
    )
    if isinstance(outcome, _Ended):
        # Returned, not raised: a sign-in ended on the way commits with it.
        return _ended(outcome.reason)
    if isinstance(outcome, str):
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": outcome}
        )
    access_token, refresh_token, user_payload = outcome

    if not service_account:
        _set_cookies_for(response, access_token, refresh_token)

    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=user_payload,
    )


def _sign_in_timing(request: Request, user: User, session: Session) -> Optional[dict]:
    """Seconds until this sign-in goes idle (None: no idle limit, including an
    idle setting of 0) and reaches its absolute end, as the server counts them.
    Relative, so the browser's clock does not matter. None when the request has
    no sign-in (dev mode)."""
    sid = _session_sid(request)
    row = session.get(SignIn, sid) if sid else None
    if row is None:
        return None
    limit = _limits(user, session)
    now = utcnow()
    return {
        "idle_in_seconds": (
            int((row.last_action_at + limit.idle - now).total_seconds())
            if limit.idle
            else None
        ),
        "ends_in_seconds": int(
            (sign_ins.end_of(row, limit.absolute) - now).total_seconds()
        ),
    }


@router.get("/me")
@mfa_setup_route
def get_current_user_info(
    request: Request,
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: UnitOfWorkSession,
):
    """
    Get current user information.

    Args:
        current_user: Current authenticated user
        session: Database session

    Returns:
        User information with permissions, and when the sign-in goes idle and ends
    """
    return {
        **_user_payload(current_user, session, _session_sid(request)),
        "sign_in": _sign_in_timing(request, current_user, session),
    }


def _apply_profile_update(
    session: Session,
    user: User,
    full_name: Optional[str],
    email: Optional[str],
    current_password: Optional[str],
    code: Optional[str] = None,
    sign_in: Optional[str] = None,
    source: Optional[str] = None,
) -> tuple[bool, dict]:
    """Apply a profile edit; return whether the email changed and the new payload.

    The mailbox is checked before any column is written. A rejected email
    change must not depend on the request transaction rolling the name back.
    """
    changing_email = bool(email) and fold(email) != fold(user.email)
    old_email = user.email if changing_email else None
    if changing_email:
        # Password reset is mailed to the address on the account, so a live
        # session is not enough to point that address somewhere else: the strong
        # check, every time, the confirmation window or not. Without MFA, the
        # password alone. 400, not 401: the session is valid and a body field is
        # wrong. A 401 would make the web client refresh and retry.
        step_up.confirm(
            user,
            session,
            sign_in,
            password=current_password,
            code=code,
            strong=True,
            source=source,
        )
        claim_sign_in_names(session, email)
        if identifier_in_use(session, email, except_user_id=user.user_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="That email is in use or reserved. Choose another.",
            )

    changed = []
    if full_name and full_name != user.full_name:
        changed.append("full_name")
    if full_name:
        user.full_name = full_name
    if changing_email:
        changed.append("email")
        user.email = email
        user.is_verified = False
        # A link mailed to the old address must not outlive it.
        invites.revoke_resets(
            session, user.principal_id, user.principal_id, "email_changed"
        )
        # A confirmation of the previous address must not confirm this one.
        if _confirm_mail_ready():
            _, token = invites.issue_confirm(session, user, user.principal_id)
            _send_confirm_link(session, user, token)
        else:
            invites.revoke_confirms(
                session, user.principal_id, user.principal_id, "email_changed"
            )

    # Flush so the read-back sees server defaults; the request's unit
    # of work commits.
    session.flush()
    session.refresh(user)
    if changed:
        trail_events.record(
            session,
            "user.updated",
            {"personal": {k: getattr(user, k) for k in changed}},
            actor=user.principal_id,
            principal=user.principal_id,
            sign_in=sign_in,
            changed=changed,
        )
    payload = UserSchema.dump(user)
    if changing_email:
        title = "Your email address was changed"
        message = _change_notice(
            session, user, "Your email address was changed", source
        )
        _tell_person(
            session, user, title, message, kind="email_changed", fallback=_DISPUTE_SHORT
        )
        if old_email:
            _tell_address(
                session,
                old_email,
                title,
                with_dispute(user.principal_id, message, _DISPUTE_SHORT, session),
            )
        if not _confirm_mail_ready():
            payload["message"] = _NO_CONFIRM_LINK
    return changing_email, payload


@router.put("/me")
@limiter.limit("5/minute", key_func=session_rate_key)
async def update_current_user(
    request: Request,
    body: ProfileUpdateRequest,
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: UnitOfWorkSession,
):
    """
    Update the signed-in user's profile.

    Args:
        request: FastAPI request (used by the rate limiter).
        body: New name, email, and — when the email changes — current password.
        current_user: Current authenticated user
        session: Database session

    Returns:
        Updated user information
    """
    # The old shape took these as query parameters. A password must not travel
    # in the URL, and a query-string email would skip the body check below.
    if "email" in request.query_params or "full_name" in request.query_params:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Send profile fields in the JSON body",
        )

    # current_user is detached; write through this session's own row.
    user = session.get(User, current_user.user_id)

    try:
        email_changed, payload = await asyncio.to_thread(
            _apply_profile_update,
            session,
            user,
            body.full_name,
            body.email,
            body.current_password,
            body.code,
            _session_sid(request),
            client_host(request),
        )

        if email_changed:
            await asyncio.to_thread(
                sign_ins.end_all_for_user,
                current_user.user_id,
                sign_ins.ACCOUNT_CHANGED,
                session,
            )

        logger.info(f"User profile updated: {current_user.username}")
        return payload

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Profile update error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update profile",
        )


@router.post("/email-confirmation")
@limiter.limit("5/minute", key_func=session_rate_key)
def resend_email_confirmation(
    request: Request,
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: UnitOfWorkSession,
):
    """Send another confirmation link for the signed-in person's address.

    Replaces the open one. Mail and the install's public address both have to
    be set; otherwise the address stays unconfirmed and the response says so.
    """
    user = session.get(User, current_user.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    if user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This email address is already confirmed",
        )
    if not _confirm_mail_ready():
        return {"message": _NO_CONFIRM_LINK}
    _, token = invites.issue_confirm(session, user, user.principal_id)
    _send_confirm_link(session, user, token)
    return {"message": "A confirmation link has been sent."}


class EmailConfirmation(BaseModel):
    """The value from the confirmation link. Opening the page does not send it."""

    token: str


_INVALID_CONFIRM = "This confirmation link isn't valid any more. Ask for a new one."


@router.post("/email-confirmation/confirm")
@limiter.limit("5/hour", key_func=confirm_email_rate_key)
def confirm_email_address(
    request: Request,
    body: EmailConfirmation,
    session: UnitOfWorkSession,
):
    """Confirm the address. The link is used up by this request, not by opening it."""
    row = invites.find(session, body.token, lock=True, kind=invites.CONFIRM)
    if not invites.usable(row):
        if row is not None:
            invites.note_expired(session, row)
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": _INVALID_CONFIRM},
        )
    user = (
        session.query(User)
        .filter(User.principal_id == row.principal_id)
        .with_for_update()
        .first()
    )
    if user is None or not user.is_active:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": _INVALID_CONFIRM},
        )
    row.used_at = utcnow()
    user.is_verified = True
    trail_events.record(
        session,
        "email.confirmed",
        {"source_ip": client_host(request)},
        actor=user.principal_id,
        principal=user.principal_id,
        link_id=row.link_id,
    )
    return {"message": "Email address confirmed."}


@router.post("/change-password")
@limiter.limit("5/minute", key_func=session_rate_key)
async def change_password(
    request: Request,
    response: Response,
    body: ChangePasswordRequest,
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: UnitOfWorkSession,
):
    """
    Change user password.

    Args:
        request: FastAPI request (used by the rate limiter).
        body: Current and new password
        current_user: Current authenticated user
        session: Database session

    Returns:
        Success message
    """
    # bcrypt and the strength check are CPU-bound, so they run off the event loop.
    if not await asyncio.to_thread(
        AuthService.verify_password, body.current_password, current_user.password_hash
    ):
        # 400, not 401: the session is valid, a body field is wrong. A 401 would
        # make the web client refresh and retry, spending two rate-limit slots.
        # Own transaction: this raise rolls the request's back.
        await asyncio.to_thread(
            trail_events.password_change_failed,
            current_user.principal_id,
            _session_sid(request),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )

    # current_user is detached; write through this session's own row.
    user = session.get(User, current_user.user_id)
    # The leak flag waives the minimum age, and only that.
    if not body.suspected_leak and (refused := _min_age_refusal(user)):
        return refused

    # Validate new password against the strength policy. Penalize passwords
    # built from the account's own identifiers.
    try:
        await asyncio.to_thread(
            validate_password_strength,
            body.new_password,
            user_inputs=[current_user.username, current_user.email],
        )
    except PasswordPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.as_detail(),
        )

    try:
        waived = body.suspected_leak and _min_age_refusal(user) is not None
        # Enforce no-reuse + hash + history rotation
        await asyncio.to_thread(_apply_new_password, user, body.new_password)
        await asyncio.to_thread(
            trail_events.record,
            session,
            "auth.password_changed",
            actor=current_user.principal_id,
            principal=current_user.principal_id,
            sign_in=_session_sid(request),
            suspected_leak=body.suspected_leak,
            min_age_waived=waived,
        )
        if body.suspected_leak:
            _tell_managers_of_leak(session, user)
        _tell_person(
            session,
            user,
            "Your password was changed",
            _change_notice(
                session, user, "Your password was changed", client_host(request)
            ),
            kind="password_changed",
            fallback=_DISPUTE_SHORT,
        )

        # End every sign-in, this one included; the client should re-login.
        # In this transaction: the change does not commit without it.
        await asyncio.to_thread(
            sign_ins.end_all_for_user,
            current_user.user_id,
            sign_ins.ACCOUNT_CHANGED,
            session,
        )

        clear_auth_cookies(response)
        logger.info(f"Password changed for user: {current_user.username}")
        return {"message": "Password changed successfully. Please log in again."}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Password change error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to change password",
        )


def _tell_managers_of_leak(session: Session, user: User) -> None:
    """After the change commits, tell the holders of users.write. No password in it."""
    principal, name = user.principal_id, user.username
    title = f"{name} reported a suspected password leak"
    message = (
        f"{name} changed their password and reported that the old one may have "
        "leaked. The minimum password age did not apply to the change."
    )

    def send() -> None:
        for to in managers(principal):
            try:
                notify(
                    to,
                    "password_leak",
                    title,
                    message,
                    USERS_LINK,
                    priority="high",
                    about=principal,
                )
            except Exception:  # noqa: BLE001 - the rest are still told
                logger.exception("could not send a password-leak notice for %s", name)

    when_committed(session, send)


def _record_mfa(session: Session, request: Request, user: User, action: str) -> None:
    trail_events.record(
        session,
        action,
        actor=user.principal_id,
        principal=user.principal_id,
        sign_in=_session_sid(request),
    )


@router.post("/mfa/setup", response_model=MFASetupResponse)
@mfa_setup_route
def setup_mfa(
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: UnitOfWorkSession,
):
    """
    Setup MFA for current user. Returns 409 if MFA is already enabled.

    Args:
        current_user: Current authenticated user
        session: Database session

    Returns:
        MFA secret and QR code URI. Recovery codes are not issued here —
        they are returned by POST /mfa/verify once the first TOTP code is
        confirmed.
    """
    # Re-running setup would silently replace the app's secret. A person with
    # only security keys is on MFA already and may still add the app.
    if mfa_methods.has_app(
        current_user, security_keys.has_keys(session, current_user.principal_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="MFA is already enabled; turn it off before setting it up again",
        )

    secret = AuthService.setup_mfa(current_user.user_id, session)
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to setup MFA",
        )

    qr_uri = AuthService.get_mfa_qr_uri(current_user.user_id, session)
    if not qr_uri:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate QR code",
        )

    return MFASetupResponse(secret=secret, qr_uri=qr_uri)


@router.post("/mfa/verify", response_model=RecoveryCodesResponse)
@mfa_setup_route
@limiter.limit("5/minute", key_func=session_rate_key)
def verify_mfa(
    request: Request,
    body: MFAVerifyRequest,
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: UnitOfWorkSession,
):
    """
    Verify the first TOTP code and enable MFA.

    Args:
        request: FastAPI request (used by the rate limiter).
        body: MFA code
        current_user: Current authenticated user
        session: Database session

    Returns:
        Success message and one-time recovery codes (shown only once).
    """
    raise_if_locked(current_user)
    # A person on MFA through security keys alone is adding the app: that is a
    # method change, so it takes the password and a key (never this code, which
    # belongs to the new secret).
    adding_app = current_user.mfa_enabled and not mfa_methods.has_app(
        current_user, security_keys.has_keys(session, current_user.principal_id)
    )
    if adding_app:
        step_up.confirm(
            current_user,
            session,
            _session_sid(request),
            password=body.password,
            assertion=body.assertion,
            challenge=body.challenge,
            strong=True,
            signed_in=_recovery_window(request, session),
            source=client_host(request),
            method_change=True,
            sso_proof=body.sso_proof,
        )
    elif not current_user.mfa_enabled:
        # The first method needs only the password (or the provider's proof); a
        # wrong one counts toward the three that end this sign-in.
        step_up.confirm(
            current_user,
            session,
            _session_sid(request),
            password=body.password,
            source=client_host(request),
            method_change=True,
            sso_proof=body.sso_proof,
        )
    if not body.code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid MFA code"
        )
    codes = AuthService.enable_mfa(current_user.user_id, body.code, session)

    if codes is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid MFA code"
        )
    # Read before this entry: "again" means an earlier on or off.
    again = mfa_methods.enabled_before(session, current_user.principal_id)
    _record_mfa(session, request, current_user, "auth.mfa_enabled")
    if adding_app:
        # Adding ends no sign-in; the notice says how to end the others.
        _tell_person(
            session,
            current_user,
            _ADDED_APP[0],
            _ADDED_APP[1],
            fallback=_DISPUTE_SIGN_OUT,
        )
    elif again and (codes or current_user.break_glass_at is not None):
        _tell_person(
            session,
            current_user,
            "Two-step sign-in was turned on again",
            "Two-step sign-in was turned on again for your account. If this "
            "wasn't you, change your password and tell an admin.",
            fallback=_DISPUTE_PASSWORD,
        )
    # Setting it up proved the person holds the factor: the window starts.
    step_up.open_window(session, _session_sid(request))

    return RecoveryCodesResponse(
        recovery_codes=codes, message="MFA enabled successfully"
    )


def _recovery_window(request: Request, session: Session) -> bool:
    """Whether this is a key-only person's recovery sign-in, still inside its
    privileged window and with keys still unavailable: the sign-in stands in for the code.
    A break-glass sign-in does for its whole life: its code was spent to get in, and
    asking for another would spend the safe's codes on adding a method.
    """
    row = session.get(SignIn, _session_sid(request) or "")
    if row and row.ended_at is None and row.auth_method == sign_ins.BREAK_GLASS:
        return True
    if row and row.auth_method == sign_ins.SSO and row.mfa_met == sign_ins.STEP_DOWN:
        # After the provider, a key-only person's recovery code is the one Vigil
        # step-down (the app's needs an app); the provider's has a stored end.
        user = session.query(User).filter(User.principal_id == row.principal_id).first()
        keyed = user is not None and security_keys.has_keys(session, user.principal_id)
        if row.ends_at is not None or not keyed or mfa_methods.has_app(user, keyed):
            return False
    elif not (row and row.auth_method == sign_ins.RECOVERY_STEP_DOWN):
        return False
    return bool(
        step_up.window_end(row.confirmed_at)
        # Once keys are back a key is asked for again.
        and not security_keys.availability().available
    )


def _strong_check(
    request: Request, user: User, body: MFAVerifyRequest, session: Session
) -> None:
    """The password and a current code, every time, inside the window too. 400 if
    MFA is off (nothing to change); a wrong answer counts, and the third ends
    this sign-in."""
    if not user.mfa_enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "MFA is not enabled")
    step_up.confirm(
        user,
        session,
        _session_sid(request),
        password=body.password,
        code=body.code,
        assertion=body.assertion,
        challenge=body.challenge,
        strong=True,
        signed_in=_recovery_window(request, session),
        source=client_host(request),
        method_change=True,
        sso_proof=body.sso_proof,
    )


@router.get("/mfa-methods", response_model=List[mfa_methods.MfaMethod])
def list_my_mfa_methods(
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: UnitOfWorkSession,
):
    """Your own sign-in methods: type, name, added and last used. Never a secret."""
    return mfa_methods.methods(session, current_user)


@router.post("/mfa/recovery-codes", response_model=RecoveryCodesResponse)
@limiter.limit("5/minute", key_func=session_rate_key)
def regenerate_recovery_codes(
    request: Request,
    body: MFAVerifyRequest,
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: UnitOfWorkSession,
):
    """
    Generate a fresh set of one-time MFA recovery codes, invalidating any
    previous codes. Requires MFA to be enabled and a current TOTP or
    recovery code.

    Returns:
        The new plaintext recovery codes, shown only once.
    """
    raise_if_locked(current_user)
    # A break-glass account's codes are the safe's; refused before the check
    # so no code is spent.
    if current_user.break_glass_at is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, break_glass.MARKED)
    _strong_check(request, current_user, body, session)
    codes = AuthService.get_mfa_recovery_codes(current_user.user_id, session)
    if codes is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="MFA is not enabled"
        )
    _record_mfa(session, request, current_user, "auth.recovery_codes_regenerated")
    _tell_person(
        session,
        current_user,
        "Your recovery codes were regenerated",
        _change_notice(
            session,
            current_user,
            "Recovery codes for your account were regenerated",
            client_host(request),
        ),
        kind="recovery_codes",
        fallback=_DISPUTE_SHORT,
    )
    _end_others_after_change(
        session, current_user, _session_sid(request), client_host(request)
    )

    return RecoveryCodesResponse(recovery_codes=codes)


@router.delete("/mfa")
@limiter.limit("5/minute", key_func=session_rate_key)
def disable_mfa(
    request: Request,
    body: MFAVerifyRequest,
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: UnitOfWorkSession,
):
    """
    Disable MFA for current user. Requires a current TOTP or recovery code.

    Args:
        request: FastAPI request (used by the rate limiter).
        body: A current authenticator code or an unused recovery code
        current_user: Current authenticated user
        session: Database session

    Returns:
        Success message
    """
    raise_if_locked(current_user)
    _strong_check(request, current_user, body, session)
    keyed = security_keys.has_keys(session, current_user.principal_id)
    if not keyed:
        _refuse_last_method(current_user, session)
    if not AuthService.disable_mfa(current_user.user_id, session):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="MFA is not enabled"
        )
    # With keys left only the app went; MFA stays on.
    _record_mfa(
        session,
        request,
        current_user,
        "auth.authenticator_app_removed" if keyed else "auth.mfa_disabled",
    )
    _end_others_after_change(
        session, current_user, _session_sid(request), client_host(request)
    )
    if keyed:
        title, what, kind = (
            "Your authenticator app was removed",
            "The authenticator app was removed from your account",
            "authenticator_removed",
        )
    else:
        title, what, kind = (
            "Two-step sign-in was turned off",
            "Two-step sign-in was turned off",
            "mfa_disabled",
        )
    _tell_person(
        session,
        current_user,
        title,
        _change_notice(session, current_user, what, client_host(request)),
        kind=kind,
        fallback=_DISPUTE_SHORT,
    )

    return {"message": "MFA disabled successfully"}


# --- Security keys (#4234) ---------------------------------------------------

_ADDED_APP = (
    "An authenticator app was added",
    "An authenticator app was added to your account. If this wasn't you, sign "
    "out everywhere else from your sign-ins in the console, change your "
    "password and tell an admin.",
)
_ADDED_KEY = (
    "A security key was added",
    "A security key was added to your account. If this wasn't you, sign out "
    "everywhere else from your sign-ins in the console, change your password "
    "and tell an admin.",
)
_REMOVED_KEY = (
    "A security key was removed",
    "A security key was removed from your account and your other sign-ins "
    "ended. If this wasn't you, change your password and tell an admin.",
)


class SecurityKeyEntry(BaseModel):
    """A key as the person sees it: never its credential id or public key."""

    key_id: str
    name: str
    added_at: OptDateTime = None
    last_used_at: OptDateTime = None


class SecurityKeysResponse(BaseModel):
    """The install's key state and the caller's keys."""

    available: bool
    # Why not, naming the hostname, when ``available`` is false.
    reason: Optional[str] = None
    keys: List[SecurityKeyEntry]


class WebAuthnOptions(BaseModel):
    """Options for the browser's ``credentials.create`` or ``.get`` and the signed
    challenge to send back with the answer."""

    options: dict
    challenge: str


class RegisterKeyRequest(MFAVerifyRequest):
    """A key to register. ``credential`` and ``registration_challenge`` come from
    the browser and from POST /mfa/keys/options; the rest is the method-change
    check."""

    name: Optional[str] = None
    credential: dict
    registration_challenge: str


class SecurityKeyAdded(BaseModel):
    key: SecurityKeyEntry
    # The first method of a person without MFA: shown once, as for the app.
    recovery_codes: Optional[List[str]] = None


def _key_entry(row: SecurityKey) -> SecurityKeyEntry:
    return SecurityKeyEntry(
        key_id=row.key_id,
        name=row.name,
        added_at=row.created_at,
        last_used_at=row.last_used_at,
    )


def _key_refusal(exc: security_keys.KeyRefused) -> HTTPException:
    code = (
        status.HTTP_409_CONFLICT
        if isinstance(exc, security_keys.KeysUnavailable)
        else status.HTTP_400_BAD_REQUEST
    )
    return HTTPException(status_code=code, detail=str(exc))


def _covered(user: User, session: Session) -> bool:
    """Whether the MFA requirement covers this person (the one coverage check)."""
    held = AuthService.get_user_permissions(user.user_id, session)
    return session_policy.mfa_covered(
        [name for name, on in held.items() if on],
        _is_service_account(user),
        session_policy.policy().require_mfa,
    )


def _refuse_last_method(user: User, session: Session) -> None:
    """A covered person's last method stays: it would lock them out at the next
    request."""
    if _covered(user, session):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Your role requires two-step sign-in. Add another method "
            "before removing your last one.",
        )


@router.get("/mfa/keys", response_model=SecurityKeysResponse)
@mfa_setup_route
def list_my_security_keys(
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: UnitOfWorkSession,
):
    """Your security keys, and whether this install offers them."""
    now = security_keys.availability()
    return SecurityKeysResponse(
        **now.dump(),
        keys=[
            _key_entry(row)
            for row in security_keys.keys_of(session, current_user.principal_id)
        ],
    )


@router.post("/mfa/keys/options", response_model=WebAuthnOptions)
@mfa_setup_route
@limiter.limit("10/minute", key_func=session_rate_key)
def security_key_registration_options(
    request: Request,
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: UnitOfWorkSession,
):
    """Options to register a key: the allowed algorithms only. 409 with the
    reason when this install has no security keys."""
    try:
        return security_keys.registration_options(session, current_user)
    except security_keys.KeyRefused as exc:
        raise _key_refusal(exc) from exc


@router.post("/mfa/keys/assertion-options", response_model=WebAuthnOptions)
@limiter.limit("10/minute", key_func=session_rate_key)
def security_key_assertion_options(
    request: Request,
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: UnitOfWorkSession,
):
    """Options to answer a check with a key ("touch your security key"): send the
    assertion and ``challenge`` as ``assertion`` and ``challenge`` to /confirm or
    any method change."""
    try:
        found = security_keys.assertion_options(
            session, current_user, security_keys.CONFIRM
        )
    except security_keys.KeyRefused as exc:
        raise _key_refusal(exc) from exc
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You have no security keys",
        )
    return found


@router.post(
    "/mfa/keys", response_model=SecurityKeyAdded, status_code=status.HTTP_201_CREATED
)
@mfa_setup_route
@limiter.limit("5/minute", key_func=session_rate_key)
def register_security_key(
    request: Request,
    body: RegisterKeyRequest,
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: UnitOfWorkSession,
):
    """Register a security key. The strong check first (the first method of a
    person without MFA needs only the password); adding ends no sign-in and
    notifies the person. A first method turns MFA on and returns recovery codes
    once."""
    raise_if_locked(current_user)
    now = security_keys.availability()
    if not now.available:
        raise _key_refusal(security_keys.KeysUnavailable(now.reason))
    first = not current_user.mfa_enabled
    if first:
        step_up.confirm(
            current_user,
            session,
            _session_sid(request),
            password=body.password,
            source=client_host(request),
            method_change=True,
            sso_proof=body.sso_proof,
        )
    else:
        _strong_check(request, current_user, body, session)
    had_keys = security_keys.has_keys(session, current_user.principal_id)
    try:
        row = security_keys.register(
            session,
            current_user,
            body.name,
            body.credential,
            body.registration_challenge,
        )
    except security_keys.KeyRefused as exc:
        raise _key_refusal(exc) from exc

    codes = None
    again = mfa_methods.enabled_before(session, current_user.principal_id)
    locked = AuthService._lock_user(session, current_user.user_id)
    if not first and not had_keys and locked.mfa_last_totp_step is None:
        # The app was confirmed before steps were kept (or a recovery code made
        # this check): with keys beside it, "no step yet" would read as pending.
        locked.mfa_last_totp_step = int(utcnow().timestamp() // 30) - 1
    if first:
        codes = AuthService.turn_on_with_key(locked)
        _record_mfa(session, request, current_user, "auth.mfa_enabled")
    _record_mfa(session, request, current_user, "auth.security_key_added")
    if not first:
        _tell_person(
            session,
            current_user,
            _ADDED_KEY[0],
            _ADDED_KEY[1],
            fallback=_DISPUTE_SIGN_OUT,
        )
    elif again:
        _tell_person(
            session,
            current_user,
            "Two-step sign-in was turned on again",
            "Two-step sign-in was turned on again for your account. If this "
            "wasn't you, change your password and tell an admin.",
            fallback=_DISPUTE_PASSWORD,
        )
    # Registering proved the person holds the key: the window starts.
    step_up.open_window(session, _session_sid(request))
    return SecurityKeyAdded(key=_key_entry(row), recovery_codes=codes)


@router.delete("/mfa/keys/{key_id}")
@limiter.limit("5/minute", key_func=session_rate_key)
def remove_security_key(
    request: Request,
    key_id: str,
    body: MFAVerifyRequest,
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: UnitOfWorkSession,
):
    """Remove one of your keys. The strong check, then the person's other sign-ins
    end. Their last method is refused when the MFA requirement covers them;
    otherwise removing a key-only person's last key turns MFA off."""
    raise_if_locked(current_user)
    _strong_check(request, current_user, body, session)
    # The person's row first, so two removals (or a removal and the app's) count
    # the methods one after the other.
    locked = AuthService._lock_user(session, current_user.user_id)
    row = (
        session.query(SecurityKey)
        .filter(
            SecurityKey.key_id == key_id,
            SecurityKey.principal_id == current_user.principal_id,
        )
        .with_for_update()
        .first()
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such security key")
    keys = security_keys.keys_of(session, current_user.principal_id)
    last = len(keys) == 1 and not mfa_methods.has_app(locked, True)
    if last:
        _refuse_last_method(current_user, session)
    session.delete(row)
    session.flush()
    _record_mfa(session, request, current_user, "auth.security_key_removed")
    if last:
        AuthService.clear_mfa(locked)
        _record_mfa(session, request, current_user, "auth.mfa_disabled")
    _end_others_after_change(
        session, current_user, _session_sid(request), client_host(request)
    )
    _tell_person(
        session,
        current_user,
        _REMOVED_KEY[0],
        _REMOVED_KEY[1],
        fallback=_DISPUTE_PASSWORD,
    )
    return {"message": "Security key removed"}


# Public self-registration was removed intentionally. An account is made by an
# admin-gated invite (POST /api/users/invites in services/api/routers/users.py,
# which checks the role against the caller's privileges) that the invitee accepts
# below, or by first-run bootstrap.


@router.post("/password-reset/request")
@limiter.limit("3/hour", key_func=reset_request_rate_key)
def password_reset_request(
    request: Request,
    body: PasswordResetRequest,
    session: UnitOfWorkSession,
):
    """
    Begin a password reset. Always returns 200 regardless of whether the
    email maps to an account — otherwise the response shape leaks which
    emails are registered.

    The actual email (with a one-hour, single-use link) is sent asynchronously
    via the configured email backend. The default ConsoleBackend sends nothing;
    with DEV_MODE on it logs the link.
    """
    settings = get_settings()
    user = session.query(User).filter(User.email == body.email).first()
    # A service account's password belongs to its program: no reset mail. Nor
    # any while single sign-on is required; the answer stays the same.
    if sso.required():
        logger.info("Password reset refused: single sign-on is required")
    elif user and sso.sso_only(session, user):
        # The provider is this account's only way in; a mailbox is not.
        logger.info("Password reset refused: %s signs in only with SSO", user.user_id)
    elif user and user.is_active and user.is_verified and not _is_service_account(user):
        _, token = invites.issue_reset(session, user, invites.SELF, user.principal_id)
        link = reset_link(default_base_url(settings), token)
        subject = "Vigil SOC — password reset"
        body_text = (
            f"Hello {user.full_name or user.username},\n\n"
            "A password reset was requested for this account. Use the link "
            "below to choose a new password. The link is valid for one hour "
            "and can only be used once.\n\n"
            f"{link}\n\n"
            "If you did not request this reset, you can ignore this email."
        )
        # Once the link's row has committed: mail never carries a link that isn't.
        to = user.email
        when_committed(
            session, lambda: send_email(to=to, subject=subject, body=body_text)
        )
        logger.info("Password reset requested for %s", user.user_id)
    elif (
        user
        and user.is_active
        and not user.is_verified
        and not _is_service_account(user)
    ):
        # Same reply as every other request. The address has not proved itself.
        logger.info("Password reset skipped; email not confirmed for %s", user.user_id)
    else:
        # Unknown address, inactive user or service account — log for ops visibility but
        # return the same response. Constant-time comparison isn't needed
        # here because the DB lookup already dominates the timing.
        logger.info("Password reset requested for unknown/inactive email")

    return {"message": _reset_request_message(settings)}


def _reset_request_message(settings) -> str:
    """What the requester is told. Depends only on how mail is configured,
    never on the account, so the reply still can't enumerate emails."""
    if mail_is_configured():
        return (
            "If that email matches an active account, a reset link has been sent. "
            "An address changed but not yet confirmed gets no link — ask an admin."
        )
    if settings.dev_mode:
        return (
            "Email isn't set up on this server. If that email matches an active "
            "account, its reset link or token has been written to the server log."
        )
    return (
        "Email isn't set up on this server, so no reset link was sent. Ask an "
        "administrator to issue one with scripts/reset-password.sh (on a compose "
        "install: docker exec deeptempo-backend python scripts/reset_password.py)."
    )


def _break_glass_link(session: Session, row) -> bool:
    """A reset link the host issued, or a sign-in's forced change, for a marked account."""
    if row is None or row.channel not in (invites.HOST, invites.SIGNIN):
        return False
    marked = (
        session.query(User.break_glass_at)
        .filter(User.principal_id == row.principal_id)
        .scalar()
    )
    return marked is not None


def _link_problem(row: Optional[invites.AuthLink]) -> str:
    """Why ``invites.usable`` failed a link, for the trail."""
    if row is None:
        return "unknown"
    if row.used_at is not None:
        return "used"
    if row.revoked_at is not None:
        return "revoked"
    return "expired" if invites.is_expired(row) else "too_many_attempts"


@router.post("/password-reset/confirm")
@limiter.limit("5/hour", key_func=reset_confirm_rate_key)
def password_reset_confirm(
    request: Request,
    body: PasswordResetConfirm,
    session: UnitOfWorkSession,
):
    """
    Complete a password reset. The link's row is locked, so two confirms of one
    link serialize and the second finds it used. Enforces the password strength
    policy and reuse check, uses the link up, closes every other open one, and
    ends the account's sign-ins. A rejected password leaves the link usable.
    """
    source = client_host(request)

    def refuse(reason, row=None):
        # Own transaction: two of the refusals raise, which rolls this one back.
        trail_events.reset_refused(
            source,
            reason,
            body.token,
            row.link_id if row else None,
            row.principal_id if row else None,
        )

    row = invites.find(session, body.token, lock=True, kind=invites.RESET)
    # A link issued before single sign-on was required is not honoured now. A
    # break-glass account's host link or its own forced-change token still is.
    if sso.required() and not _break_glass_link(session, row):
        refuse("sso_required")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=sso.REQUIRED_DETAIL
        )
    if not invites.usable(row):
        # First: note_expired holds the trail's lock until this request ends,
        # which the refusal's own transaction would wait on.
        refuse(_link_problem(row), row)
        if row is not None:
            invites.note_expired(session, row)
        # Returned, not raised, so the expiry's trail entry stays.
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": _INVALID_RESET_TOKEN},
        )

    user = (
        session.query(User)
        .filter(User.principal_id == row.principal_id)
        .with_for_update()
        .first()
    )
    if user is not None and _is_service_account(user):
        logger.warning("Refused password reset for service account %s", user.username)
        refuse("not_eligible", row)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_INVALID_RESET_TOKEN,
        )
    if not user or not user.is_active:
        refuse("not_eligible", row)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account no longer eligible for reset",
        )
    # A self-service link never gives an SSO-only account a password; an
    # administrator's or the host script's still can.
    if row.channel == invites.SELF and sso.sso_only(session, user):
        refuse("sso_required", row)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_INVALID_RESET_TOKEN,
        )

    # The minimum age limits a voluntary change only: not the reset sign-in
    # forced or an administrator issued, nor one that unlocks a locked account,
    # nor one whose password can't be read (a retired pepper).
    unreadable = password_hash.unreadable(user.password_hash)
    if not invites.forced(row) and not unreadable and not _account_is_locked(user):
        if refused := _min_age_refusal(user):
            return refused

    try:
        validate_password_strength(
            body.new_password, user_inputs=[user.username, user.email]
        )
    except PasswordPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.as_detail(),
        )

    row.used_at = utcnow()
    unread = _apply_new_password(user, body.new_password)
    # Clear any active lockout so the user can immediately log in.
    clear_lock(user)
    # The link's holder acted; they have no session.
    trail_events.record(
        session,
        "auth.password_reset",
        {"source_ip": client_host(request)},
        actor=user.principal_id,
        principal=user.principal_id,
        link_id=row.link_id,
        channel=row.channel,
        **unread,
    )
    sign_ins.end_all_for_user(user.user_id, sign_ins.ACCOUNT_CHANGED, session)
    _tell_person(
        session,
        user,
        "Your password was reset",
        "Your password was reset through a reset link and your sign-ins ended. "
        "If this wasn't you, tell an admin now.",
        kind="password_reset",
        fallback=_DISPUTE_NOW,
    )
    logger.info("Password reset completed for user: %s", user.username)
    return {
        "message": "Password reset successfully",
        "recovery_codes_removed": unread["recovery_codes_removed"],
    }


class InviteCheck(BaseModel):
    """What the accept screen shows: the fixed address and a username to start from."""

    email: str
    username: str


class AcceptInvite(BaseModel):
    token: str
    username: str
    password: str


_INVITE_STALE = (
    "The person who sent this invite can't grant this role any more. "
    "Ask for a new invite."
)


def _invite_refused(detail: str) -> JSONResponse:
    # Returned, not raised: a raise rolls back what the refusal recorded.
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST, content={"detail": detail}
    )


def _invite_problem(session: Session, row) -> Optional[str]:
    """Why this link can't be used now, or None. The inviter has to still be an
    active person whose permissions cover the role, and the role has to exist."""
    from services.api.routers.users import (
        _is_erased,
        _is_person_account,
        _uncovered,
    )

    if not invites.usable(row):
        invites.note_expired(session, row)
        return invites.INVALID
    inviter = (
        session.query(User, Principal.deleted_at)
        .join(Principal, Principal.principal_id == User.principal_id)
        .filter(User.principal_id == row.invited_by)
        .first()
    )
    role = session.get(Role, row.role_id)
    if inviter is None or role is None:
        return _INVITE_STALE
    user, deleted_at = inviter
    if (
        deleted_at is not None
        or not user.is_active
        or _is_erased(user)
        or not _is_person_account(user, session)
        or not AuthService.check_permission(user.user_id, "users.write", session)
        or _uncovered(user, role.permissions, session)
    ):
        return _INVITE_STALE
    return None


@router.get("/invite", response_model=InviteCheck)
@limiter.limit("30/minute", key_func=invite_rate_key)
def invite_check(
    request: Request,
    session: UnitOfWorkSession,
    x_invite_token: Annotated[str, Header()] = "",
):
    """Check an invite link for the accept screen. Opening it uses nothing up.
    The value travels in a header, so no log line carries it."""
    row = invites.find(session, x_invite_token) if x_invite_token else None
    if row is None:
        return _invite_refused(invites.INVALID)
    if problem := _invite_problem(session, row):
        return _invite_refused(problem)
    return InviteCheck(email=row.email, username=invites.suggested_username(row.email))


@router.post("/accept-invite")
@limiter.limit("10/minute", key_func=invite_rate_key)
def accept_invite(
    request: Request,
    body: AcceptInvite,
    session: UnitOfWorkSession,
):
    """Set up the account an invite is for. The submission that creates it uses
    the link up; a refused password or a taken username leaves it usable, up to
    ``invites.MAX_ATTEMPTS`` tries. Signing in is the normal login afterwards."""
    # Locked, so two submissions of one link can't both create an account.
    row = invites.find(session, body.token, lock=True) if body.token else None
    if row is None:
        return _invite_refused(invites.INVALID)
    if problem := _invite_problem(session, row):
        return _invite_refused(problem)

    def refused(reason: str, detail: str) -> JSONResponse:
        row.attempts += 1
        trail_events.record(
            session,
            "invite.failed",
            {"source_ip": client_host(request), "personal": {"email": row.email}},
            actor=SYSTEM,
            link_id=row.link_id,
            reason=reason,
            attempts=row.attempts,
        )
        return _invite_refused(detail)

    username = body.username.strip()
    if bad := invites.username_problem(username):
        return refused("username_invalid", bad)
    claim_sign_in_names(session, username, row.email)
    if identifier_in_use(session, username, except_link_id=row.link_id):
        return refused(
            "username_taken", "That username is in use or reserved. Choose another."
        )
    try:
        validate_password_strength(body.password, user_inputs=[username, row.email])
    except PasswordPolicyError as exc:
        return refused("weak_password", exc.as_detail())

    user = AuthService.create_user(
        username=username,
        email=row.email,
        password=body.password,
        full_name=username,
        role_id=row.role_id,
        session=session,
        invite_link_id=row.link_id,
    )
    if user is None:
        return refused("address_in_use", _IN_USE_OR_RESERVED)
    row.used_at = utcnow()
    trail_events.record(
        session,
        "user.created",
        {"personal": trail_events.personal(user)},
        actor=user.principal_id,
        principal=user.principal_id,
        role_id=user.role_id,
        via="invite",
        invited_by=row.invited_by,
    )
    trail_events.record(
        session,
        "invite.accepted",
        {"source_ip": client_host(request), "personal": {"email": row.email}},
        actor=user.principal_id,
        principal=user.principal_id,
        link_id=row.link_id,
        role_id=user.role_id,
        invited_by=row.invited_by,
    )
    # The account holds the address now; the used link keeps only who it became.
    row.principal_id = user.principal_id
    row.email = None
    inviter = row.invited_by
    when_committed(
        session,
        lambda: notify(
            inviter,
            "invite_accepted",
            "Invite accepted",
            f"{username} accepted your invite and now has an account.",
            link="/settings",
        ),
    )
    logger.info("Invite %s accepted: account %s created", row.link_id, user.username)
    return {"message": "Account created. Sign in with your new password."}


# Read by _refuse_service_account at request time; the endpoints are defined above.
_SERVICE_ACCOUNT_ENDPOINTS = frozenset({login, refresh_token})
