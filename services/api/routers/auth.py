"""
Authentication API - User authentication endpoints.

Handles login, logout, token refresh, password management, and MFA.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Annotated, List, NamedTuple, Optional, Union

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from core.audit.trail import SYSTEM
from core.auth import (
    mfa_methods,
    security_keys,
    session_policy,
    sign_ins,
    step_up,
    trail_events,
)
from core.auth.auth_cookies import (
    ACCESS_COOKIE_NAME,
    REFRESH_COOKIE_NAME,
    clear_auth_cookies,
    set_auth_cookies,
)
from core.auth.auth_service import (
    CODE_APP,
    CODE_KEY,
    CODE_RECOVERY,
    AccountLockedError,
    AuthService,
    _account_is_locked,
    _is_service_account,
    clear_lock,
    managers,
    password_matches_any,
    raise_if_locked,
)
from core.auth.bootstrap_code import has_human_user, setup_code_matches
from core.auth.current_user import DEV_MODE, _detached, mfa_setup_route, setup_needed
from core.auth.deactivation import when_committed
from core.auth.identity import claim_sign_in_names, identifier_in_use
from core.auth.mcp_credential_service import looks_like_api_token
from core.auth.password_reset import (
    decode_reset_token,
    default_base_url,
    generate_reset_token,
    is_current,
    reset_link,
)
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
from core.storage.models import SecurityKey, SignIn, User
from core.storage.schemas import UserSchema
from core.storage.schemas.base import IsoDateTime, OptDateTime
from core.time import utcnow
from services.api.middleware.auth import get_current_active_user
from services.api.middleware.auth_rate_key import (
    bootstrap_rate_key,
    login_rate_key,
    refresh_rate_key,
    reset_confirm_rate_key,
    reset_request_rate_key,
    session_rate_key,
)
from services.api.middleware.client_address import client_host
from services.api.middleware.rate_limit import limiter

logger = logging.getLogger(__name__)

# A service account (Medic's gateway, D2-17b) may log in and refresh through the
# API and nothing else here, so a leaked password can't become a console session.
SERVICE_ACCOUNT_DETAIL = "Service accounts can't use this endpoint"
_INVALID_REFRESH_TOKEN = "Invalid refresh token"
_INVALID_RESET_TOKEN = "Invalid or expired reset token"
# The 400 body's code when the minimum password age refuses a change.
PASSWORD_MIN_AGE = "password_min_age"
# Login's 403 detail.code when the password must change before sign-in.
PASSWORD_CHANGE_REQUIRED = "password_change_required"
# A password under this many characters is not enough on its own, without MFA.
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


class PasswordResetRequest(BaseModel):
    """Password reset initiation."""

    email: EmailStr


class PasswordResetConfirm(BaseModel):
    """Password reset completion."""

    token: str
    new_password: str


def _apply_new_password(user: User, plaintext: str) -> None:
    """Enforce history, hash, set, update history + changed_at in one place.
    The request's unit of work owns the commit."""
    keep = session_policy.policy().password_history
    # The current hash and the history are checked together (PBKDF2 runs once
    # per distinct salt).
    if password_matches_any(
        plaintext, [user.password_hash, *(user.password_history or [])[:keep]]
    ):
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
    """The session limits for this account, by the permissions it holds."""
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
    if row.started_at + limit.absolute <= now:
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

    access_token = AuthService.generate_jwt_token(user, "access", sid=sid)
    refresh_token = AuthService.generate_jwt_token(
        user,
        "refresh",
        sid=sid,
        generation=current,
        not_after=row.started_at + limit.absolute,
    )
    refresh_payload = AuthService.verify_jwt_token(refresh_token) or {}
    row.expires_at = _exp_datetime(refresh_payload.get("exp"))
    return access_token, refresh_token, _user_payload(user, session)


def _user_payload(user: User, session: Session) -> dict:
    """User dict plus resolved permissions — the shape the SPA gates on.
    Login/refresh must include it, not just /me: the client stores the user
    from the login response and checks permissions before any /me refresh."""
    payload = UserSchema.dump(user)
    payload["permissions"] = AuthService.get_user_permissions(user.user_id, session)
    # The console holds this person at MFA setup until it is done.
    # (Not in dev mode, which has no gate and no user row to set MFA up on.)
    payload["mfa_setup_required"] = not DEV_MODE and bool(
        setup_needed(
            session,
            user,
            [name for name, on in payload["permissions"].items() if on],
            session_policy.policy(),
        )
    )
    # Whether this install has security keys, and why not when it has none.
    payload["security_keys"] = security_keys.availability().dump()
    return payload


@router.get("/bootstrap", response_model=BootstrapStatusResponse)
def bootstrap_status(session: UnitOfWorkSession):
    """Report whether this instance has no account yet.

    There is no self-service signup and creating a user needs users.write, so
    an instance with an empty user table cannot be signed into at all. The
    login screen asks this to offer first-account creation instead.
    """
    return BootstrapStatusResponse(required=not has_human_user(session))


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
        # Nothing to collide with on an empty instance except a racing bootstrap.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Could not create the account. Try again.",
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
    session: Session, user: User, keep: Optional[str], source: Optional[str]
) -> None:
    """A method change ends the person's other sign-ins and stops their runs;
    ``keep``, the one making the change, stays open with its runs."""
    ids = [sid for sid, _ in sign_ins.open_ids(session, user.principal_id, keep=keep)]
    sign_ins.end_all_for_user(
        user.user_id,
        sign_ins.ACCOUNT_CHANGED,
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
            end_reason=sign_ins.ACCOUNT_CHANGED,
        )


def _tell_person(session: Session, user: User, title: str, message: str) -> None:
    """Tell the person once the change commits. Never a code: notices are emailed."""
    principal = user.principal_id

    def send() -> None:
        notify(
            principal, "mfa_change", title, message, link="/settings", priority="high"
        )

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
    _end_others_after_change(session, user, None, source)
    _tell_person(
        session,
        user,
        "A recovery code was used to sign in",
        "A recovery code was used to sign in to your account. Your authenticator "
        "app and remaining recovery codes were removed and your other sign-ins "
        "ended; two-step sign-in has to be set up again. If this wasn't you, "
        "change your password and tell an admin.",
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
                )
            except Exception:  # noqa: BLE001 - the rest are still told
                logger.exception("could not send a recovery notice for %s", name)

    when_committed(session, tell_managers)


def _mfa_required(
    session: Session, user: User, key_required: bool = False
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
        content={"detail": detail, "methods": methods, "security_key": challenge},
        headers={"X-MFA-Required": "true"},
    )


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

    def send() -> None:
        try:
            notify(
                principal,
                SIGN_IN_LIMIT_KIND,
                "A new sign-in ended your oldest one",
                EVICTION_TEXT,
                link="/overview?sign_ins=1",
                priority="high",
                metadata={"sign_in_id": sid},
                mail_body=f"{EVICTION_TEXT} It came from {where}.",
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

    if not user:
        failed()
        raise _invalid_credentials()

    service_account = _is_service_account(user)
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
    # Check MFA if enabled (a method of any kind: app, recovery codes or a key)
    if user.mfa_enabled:
        if not payload.mfa_code and not payload.webauthn:
            return _mfa_required(session, user, key_required)

        # authenticate_user clears the lockout counter on this session before
        # the code is checked. verify_mfa_code opens a savepoint, and
        # SQLAlchemy flushes pending changes before that SAVEPOINT, so the
        # cleared counter would lock the row until this request ends and the
        # failure count on the other connection would wait out the request.
        # Drop the pending reset first. Put it back only if the code is good.
        pending_reset = user in session
        if pending_reset:
            session.expire(user)
        # The login step is the one place a wrong code counts toward a lock.
        if payload.webauthn:
            try:
                # The lock is authoritative, as it is for a code.
                if _account_is_locked(user):
                    raise security_keys.KeyRefused("locked")
                security_keys.verify_assertion(
                    session,
                    user,
                    payload.webauthn,
                    payload.webauthn_challenge,
                    security_keys.SIGN_IN,
                )
                accepted = CODE_KEY
            except security_keys.KeyRefused as exc:
                accepted = None
                # A key switched off by configuration is not a wrong key.
                if not isinstance(exc, security_keys.KeysUnavailable):
                    AuthService._count_mfa_failure(user.user_id, user.username, source)
        else:
            accepted = AuthService.verify_mfa_code(
                user.user_id,
                payload.mfa_code,
                session,
                count_failures=True,
                source=source,
            )
        if not accepted:
            failed("bad_security_key" if payload.webauthn else "bad_mfa_code")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    "Invalid security key" if payload.webauthn else "Invalid MFA code"
                ),
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
        if pending_reset:
            clear_lock(user)
            user.last_login = utcnow()
            user.login_count = (user.login_count or 0) + 1
        key_only = keyed and not mfa_methods.has_app(user, keyed)
        if accepted == CODE_RECOVERY and key_required and key_only and not keys_up:
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
        new_recovery_codes = AuthService.upgrade_recovery_codes(user)

    # The password and any MFA code are good; the password may have joined the
    # breached list since it was set, be too short for one factor, or be past the
    # maximum age. No tokens, cookies or sign-in row: a reset
    # token takes the person to /password-reset/confirm, the one forced-change
    # path. Returned, not raised, so a spent MFA code stays spent. A service
    # account's password is generated and it cannot reset.
    if not service_account:
        try:
            check_not_listed(payload.password, user_inputs=[user.username, user.email])
            check_not_expired(user)
            if not had_mfa and len(payload.password) < SINGLE_FACTOR_MIN:
                raise PasswordPolicyError(
                    f"Password is under {SINGLE_FACTOR_MIN} characters, too short "
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
                        "reset_token": generate_reset_token(user, forced=True),
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
    sign_ins.start(
        session,
        sid,
        user.principal_id,
        _exp_datetime(refresh_payload.get("exp")),
        auth_method=auth_method,
        user_agent=_ua,
        address=source,
    )
    if evicted:
        _tell_of_eviction(session, user, sid, _ua, source)
    trail_events.record(
        session,
        "auth.sign_in",
        {"source_ip": source},
        actor=user.principal_id,
        sign_in=sid,
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
    window and its count of failures in a row."""
    step_up.confirm(
        user,
        session,
        _session_sid(request),
        password=body.password,
        code=body.code,
        assertion=body.assertion,
        challenge=body.challenge,
        source=client_host(request),
    )


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
    """End these sign-ins of the caller's, then write their entries (the rows
    are locked before the trail's lock, the order every path takes)."""
    ended = [sid for sid in ids if sign_ins.end(sid, sign_ins.ENDED_BY_PERSON, session)]
    for sid in ended:
        trail_events.record(
            session,
            "auth.sign_in_ended",
            {"source_ip": client_host(request)},
            actor=user.principal_id,
            principal=user.principal_id,
            sign_in=sid,
            end_reason=sign_ins.ENDED_BY_PERSON,
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
    """Seconds until this sign-in goes idle (None: no idle limit) and reaches its
    absolute end, as the server counts them. Relative, so the browser's clock
    does not matter. None when the request has no sign-in (dev mode)."""
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
        "ends_in_seconds": int((row.started_at + limit.absolute - now).total_seconds()),
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
        **_user_payload(current_user, session),
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
    changing_email = bool(email) and email.casefold() != user.email.casefold()
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
                detail="Email already in use",
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
    return changing_email, UserSchema.dump(user)


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
                notify(to, "password_leak", title, message, USERS_LINK, priority="high")
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
        )
    elif not current_user.mfa_enabled:
        # The first method needs only the password; a wrong one counts toward
        # the three that end this sign-in.
        step_up.confirm(
            current_user,
            session,
            _session_sid(request),
            password=body.password,
            source=client_host(request),
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
        _tell_person(session, current_user, *_ADDED_APP)
    elif again and codes:
        _tell_person(
            session,
            current_user,
            "Two-step sign-in was turned on again",
            "Two-step sign-in was turned on again for your account. If this "
            "wasn't you, change your password and tell an admin.",
        )
    # Setting it up proved the person holds the factor: the window starts.
    step_up.open_window(session, _session_sid(request))

    return RecoveryCodesResponse(
        recovery_codes=codes, message="MFA enabled successfully"
    )


def _recovery_window(request: Request, session: Session) -> bool:
    """Whether this is a key-only person's recovery sign-in, still inside its
    privileged window and with keys still unavailable: the sign-in stands in for the code.
    """
    row = session.get(SignIn, _session_sid(request) or "")
    return bool(
        row
        and row.auth_method == sign_ins.RECOVERY_STEP_DOWN
        and step_up.window_end(row.confirmed_at)
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
    _strong_check(request, current_user, body, session)
    codes = AuthService.get_mfa_recovery_codes(current_user.user_id, session)
    if codes is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="MFA is not enabled"
        )
    _record_mfa(session, request, current_user, "auth.recovery_codes_regenerated")
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
        _tell_person(session, current_user, *_ADDED_KEY)
    elif again:
        _tell_person(
            session,
            current_user,
            "Two-step sign-in was turned on again",
            "Two-step sign-in was turned on again for your account. If this "
            "wasn't you, change your password and tell an admin.",
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
    _tell_person(session, current_user, *_REMOVED_KEY)
    return {"message": "Security key removed"}


# Public self-registration was removed intentionally. All user creation
# goes through the admin-gated POST /api/users/ endpoint (services/api/routers/users.py)
# which validates the requested role against the caller's privileges.


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

    The actual email (with the signed reset token) is sent asynchronously
    via the configured email backend. The default ConsoleBackend sends nothing;
    with DEV_MODE on it logs the link.
    """
    settings = get_settings()
    user = session.query(User).filter(User.email == body.email).first()
    # A service account's password belongs to its program: no reset mail.
    if user and user.is_active and not _is_service_account(user):
        token = generate_reset_token(user)
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
        send_email(to=user.email, subject=subject, body=body_text)
        logger.info("Password reset requested for %s", user.user_id)
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
        return "If that email matches an active account, a reset link has been sent."
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


@router.post("/password-reset/confirm")
@limiter.limit("5/hour", key_func=reset_confirm_rate_key)
async def password_reset_confirm(
    request: Request,
    body: PasswordResetConfirm,
    session: UnitOfWorkSession,
):
    """
    Complete a password reset. Validates the signed token and that the
    password hasn't changed since it was issued, enforces the password
    strength policy + reuse check, rotates the hash (which voids this and
    every other outstanding reset link), and revokes every outstanding
    token so any existing sessions die. A rejected password leaves the
    token usable.
    """
    claim = decode_reset_token(body.token)
    if not claim:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_INVALID_RESET_TOKEN,
        )

    # Locked so two confirms of one link serialize: the second reads the
    # password_changed_at the first wrote, and fails is_current.
    user = await asyncio.to_thread(_get_user, session, claim.user_id, True)
    if user is not None and _is_service_account(user):
        logger.warning("Refused password reset for service account %s", user.username)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_INVALID_RESET_TOKEN,
        )
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account no longer eligible for reset",
        )
    if not is_current(claim, user):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_INVALID_RESET_TOKEN,
        )

    # The minimum age limits a voluntary change only: not the reset sign-in
    # forced, nor one that unlocks a locked account.
    if not claim.forced and not _account_is_locked(user):
        if refused := _min_age_refusal(user):
            return refused

    try:
        await asyncio.to_thread(
            validate_password_strength,
            body.new_password,
            user_inputs=[user.username, user.email],
        )
    except PasswordPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.as_detail(),
        )

    try:
        await asyncio.to_thread(_apply_new_password, user, body.new_password)
        # Clear any active lockout so the user can immediately log in.
        clear_lock(user)
        # The reset token's holder acted; they have no session.
        await asyncio.to_thread(
            trail_events.record,
            session,
            "auth.password_reset",
            {"source_ip": client_host(request)},
            actor=user.principal_id,
            principal=user.principal_id,
        )

        await asyncio.to_thread(
            sign_ins.end_all_for_user, user.user_id, sign_ins.ACCOUNT_CHANGED, session
        )

        logger.info("Password reset completed for user: %s", user.username)
        return {"message": "Password reset successfully"}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Password reset error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reset password",
        )


# Read by _refuse_service_account at request time; the endpoints are defined above.
_SERVICE_ACCOUNT_ENDPOINTS = frozenset({login, refresh_token})
