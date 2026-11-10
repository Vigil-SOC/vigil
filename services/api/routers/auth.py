"""
Authentication API - User authentication endpoints.

Handles login, logout, token refresh, password management, and MFA.
"""

import asyncio
import logging
from datetime import datetime
from typing import Annotated, List, NamedTuple, Optional, Union

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.audit.trail import SYSTEM
from core.auth import session_policy, sign_ins, step_up, trail_events
from core.auth.auth_cookies import (
    ACCESS_COOKIE_NAME,
    REFRESH_COOKIE_NAME,
    clear_auth_cookies,
    set_auth_cookies,
)
from core.auth.auth_service import (
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
from core.auth.current_user import DEV_MODE, _detached, mfa_setup_route
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
from core.platform.notices import notify
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import SignIn, User
from core.storage.schemas import UserSchema
from core.storage.schemas.base import IsoDateTime
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


class LoginResponse(BaseModel):
    """Login response."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: dict
    # Set once, at the first MFA sign-in after the upgrade: the old recovery
    # codes are void and these replace them.
    recovery_codes: Optional[List[str]] = None


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
    """MFA verification request."""

    code: str


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
    payload["mfa_setup_required"] = (
        not DEV_MODE
        and not user.mfa_enabled
        and session_policy.mfa_covered(
            [name for name, on in payload["permissions"].items() if on],
            _is_service_account(user),
            session_policy.policy().require_mfa,
        )
    )
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
    # Check MFA if enabled
    if user.mfa_enabled:
        if not payload.mfa_code:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="MFA code required",
                headers={"X-MFA-Required": "true"},
            )

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
        if not AuthService.verify_mfa_code(
            user.user_id,
            payload.mfa_code,
            session,
            count_failures=True,
            source=source,
        ):
            failed("bad_mfa_code")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid MFA code",
            )
        if pending_reset:
            clear_lock(user)
            user.last_login = utcnow()
            user.login_count = (user.login_count or 0) + 1
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
            if not user.mfa_enabled and len(payload.password) < SINGLE_FACTOR_MIN:
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
    sign_ins.make_room(session, user.principal_id, limit.sign_in_limit)
    sign_ins.start(
        session,
        sid,
        user.principal_id,
        _exp_datetime(refresh_payload.get("exp")),
        auth_method="mfa" if user.mfa_enabled else "password",
        user_agent=_ua,
        address=source,
    )
    trail_events.record(
        session,
        "auth.sign_in",
        {"source_ip": source},
        actor=user.principal_id,
        sign_in=sid,
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
    # Re-running setup would silently replace the secret and switch MFA off.
    if current_user.mfa_enabled:
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
    codes = AuthService.enable_mfa(current_user.user_id, body.code, session)

    if codes is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid MFA code"
        )
    _record_mfa(session, request, current_user, "auth.mfa_enabled")
    # Setting it up proved the person holds the factor: the window starts.
    step_up.open_window(session, _session_sid(request))

    return RecoveryCodesResponse(
        recovery_codes=codes, message="MFA enabled successfully"
    )


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
    codes = AuthService.get_mfa_recovery_codes(current_user.user_id, body.code, session)
    if codes is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid MFA code or MFA is not enabled",
        )
    _record_mfa(session, request, current_user, "auth.recovery_codes_regenerated")

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
    if not AuthService.disable_mfa(current_user.user_id, body.code, session):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid MFA code or MFA is not enabled",
        )
    _record_mfa(session, request, current_user, "auth.mfa_disabled")

    return {"message": "MFA disabled successfully"}


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
    if user and user.is_active and user.password_hash and not _is_service_account(user):
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
    if user is not None and (_is_service_account(user) or not user.password_hash):
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
