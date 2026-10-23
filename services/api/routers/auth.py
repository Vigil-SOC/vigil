"""
Authentication API - User authentication endpoints.

Handles login, logout, token refresh, password management, and MFA.
"""

import asyncio
import logging
from datetime import datetime
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.auth import sign_ins
from core.auth.auth_cookies import (
    ACCESS_COOKIE_NAME,
    REFRESH_COOKIE_NAME,
    clear_auth_cookies,
    set_auth_cookies,
)
from core.auth.auth_service import (
    PASSWORD_HISTORY_LIMIT,
    AccountLockedError,
    AuthService,
    _is_service_account,
    password_matches_any,
)
from core.auth.bootstrap_code import has_human_user, setup_code_matches
from core.auth.password_reset import (
    generate_reset_token,
    reset_link,
    verify_reset_token,
)
from core.auth.password_validator import (
    PasswordPolicyError,
    validate_password_strength,
)
from core.auth.session_cutoff import issued_before_cutoff, stamp_session_cutoff
from core.auth.token_blacklist import (
    blacklist_jti,
    consume_refresh_jti,
    is_token_revoked,
    revoke_all_for_user,
)
from core.config import get_settings
from core.platform.email_service import mail_is_configured, send_email
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import SignIn, User
from core.storage.schemas import UserSchema
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
from services.api.middleware.rate_limit import limiter

logger = logging.getLogger(__name__)

# A service account (Medic's gateway, D2-17b) may log in and refresh through the
# API and nothing else here, so a leaked password can't become a console session.
SERVICE_ACCOUNT_DETAIL = "Service accounts can't use this endpoint"
_INVALID_REFRESH_TOKEN = "Invalid refresh token"
_INVALID_RESET_TOKEN = "Invalid or expired reset token"


def _bearer_token(request: Request) -> Optional[str]:
    parts = (request.headers.get("authorization") or "").split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


async def _refuse_service_account(request: Request, session: UnitOfWorkSession):
    """Refuse a service account's token on every route but login and refresh.

    On the whole router, so a route added later can't leave it out. It never
    authenticates: a missing or invalid token passes, and the route's own
    dependency (or none, for the public routes) decides as before. Both the
    access cookie and the bearer are checked, whichever the route would use.
    The header is read off the request so it stays out of the OpenAPI schema.
    """
    if request.scope.get("endpoint") in _SERVICE_ACCOUNT_ENDPOINTS:
        return
    for raw in {request.cookies.get(ACCESS_COOKIE_NAME), _bearer_token(request)}:
        payload = AuthService.verify_jwt_token(raw) if raw else None
        user_id = payload.get("user_id") if payload else None
        if not user_id:
            continue
        user = await asyncio.to_thread(_get_user, session, user_id)
        if user is not None and _is_service_account(user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=SERVICE_ACCOUNT_DETAIL
            )


router = APIRouter(dependencies=[Depends(_refuse_service_account)])

ROUTER_META = RouterMeta(
    prefix="/api/auth",
    tags=["authentication"],
    auth=Auth.ROUTER_MANAGED,
    reason=(
        "A deliberate mix. login / refresh / password-reset / bootstrap "
        "cannot require auth (chicken-and-egg) and are listed in "
        "PUBLIC_API_PATHS; the inner /me, /change-password and /mfa routes "
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


def _reject_if_locked(user: User) -> None:
    """423 when the account is already locked. A missing attribute (tests
    that build a stand-in user) is not a lock."""
    until = getattr(user, "locked_until", None)
    if until is None or until <= utcnow():
        return
    retry_after = max(1, int((until - utcnow()).total_seconds()))
    raise HTTPException(
        status_code=status.HTTP_423_LOCKED,
        detail="Account locked due to repeated failed login attempts",
        headers={"Retry-After": str(retry_after)},
    )


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


class ChangePasswordRequest(BaseModel):
    """Change password request."""

    current_password: str
    new_password: str


class ProfileUpdateRequest(BaseModel):
    """Signed-in user's own profile.

    ``current_password`` is required when ``email`` names a different mailbox.
    The password is a body field so it is not written to access logs.
    """

    full_name: Optional[str] = None
    email: Optional[EmailStr] = None
    current_password: Optional[str] = None


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
    if password_matches_any(plaintext, user.password_history or []) or (
        user.password_hash
        and AuthService.verify_password(plaintext, user.password_hash)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Password matches one of your last {PASSWORD_HISTORY_LIMIT} "
                "passwords — choose a different one."
            ),
        )

    new_hash = AuthService.hash_password(plaintext)
    previous = list(user.password_history or [])
    if user.password_hash:
        previous.insert(0, user.password_hash)
    # Cap to the configured limit so the JSONB row doesn't grow unbounded.
    user.password_history = previous[:PASSWORD_HISTORY_LIMIT]
    user.password_hash = new_hash
    user.password_changed_at = utcnow()


def _get_user(session: Session, user_id: str) -> Optional[User]:
    return session.query(User).filter(User.user_id == user_id).first()


def _open_sign_in(session: Session, sid: str, user: User) -> Optional[SignIn]:
    """The sign-in a refresh continues, or None if it has ended or is not
    this user's. Locked so a concurrent logout cannot end it mid-refresh."""
    row = session.get(SignIn, sid, with_for_update=True)
    if row is None or row.ended_at is not None or row.principal_id != user.principal_id:
        return None
    return row


def _user_payload(user: User, session: Session) -> dict:
    """User dict plus resolved permissions — the shape the SPA gates on.
    Login/refresh must include it, not just /me: the client stores the user
    from the login response and checks permissions before any /me refresh."""
    payload = UserSchema.dump(user)
    payload["permissions"] = AuthService.get_user_permissions(user.user_id, session)
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
    # Authenticate user
    try:
        user = AuthService.authenticate_user(
            payload.username_or_email, payload.password, session
        )
    except AccountLockedError as exc:
        retry_after = max(1, int((exc.locked_until - utcnow()).total_seconds()))
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="Account locked due to repeated failed login attempts",
            headers={"Retry-After": str(retry_after)},
        )

    if not user:
        raise _invalid_credentials()

    service_account = _is_service_account(user)
    if service_account and _is_browser_request(request):
        # Exactly a wrong password, so the console can't tell the account apart.
        # Not a login either: drop authenticate_user's pending last_login /
        # login_count / lockout reset (the 401 also rolls the request back).
        if user in session:
            session.expire(user)
        logger.warning("Refused console sign-in for service account %s", user.username)
        raise _invalid_credentials()

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
        if not AuthService.verify_mfa_code(user.user_id, payload.mfa_code, session):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid MFA code",
            )
        if pending_reset:
            user.failed_login_count = 0
            user.locked_until = None
            user.last_login = utcnow()
            user.login_count = (user.login_count or 0) + 1

    # Generate tokens. One sign-in, named on both, kept through every refresh.
    _ua = request.headers.get("user-agent")
    sid = sign_ins.new_id()
    access_token = AuthService.generate_jwt_token(
        user, "access", user_agent=_ua, sid=sid
    )
    refresh_token = AuthService.generate_jwt_token(
        user, "refresh", user_agent=_ua, sid=sid
    )

    refresh_payload = AuthService.verify_jwt_token(refresh_token) or {}
    sign_ins.start(
        session, sid, user.principal_id, _exp_datetime(refresh_payload.get("exp"))
    )
    # A service account gets body tokens only: a cookie is a console session.
    if not service_account:
        _set_cookies_for(response, access_token, refresh_token)

    logger.info(f"User logged in: {user.username}")

    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=_user_payload(user, session),
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


async def _revoke_token(raw: str, owner: User, kind: str) -> tuple[bool, Optional[str]]:
    """Blacklist a token and return whether that write happened, plus its sign-in id.

    Another user's token is left alone. A token that has already expired counts
    as revoked.
    """
    payload = AuthService.verify_jwt_token(raw)
    jti = payload.get("jti") if payload else None
    if not jti or payload.get("user_id") != owner.user_id:
        return True, None
    try:
        await blacklist_jti(jti, _exp_datetime(payload.get("exp")))
    except Exception as exc:
        logger.error(
            "Failed to blacklist %s token for %s: %s", kind, owner.username, exc
        )
        return False, payload.get("sid")
    return True, payload.get("sid")


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    body: Optional[RefreshTokenRequest] = None,
    current_user: User = Depends(get_current_active_user),
    authorization: Optional[str] = Header(None),
):
    """
    Logout user: blacklist the current access token and the refresh token so
    replaying either returns 401 for the rest of its lifetime, end their
    sign-in, and clear the HttpOnly auth cookies. An ended sign-in cannot be
    refreshed even when the blacklist write did not happen. A token with no
    sign-in is revoked only by that write, so if it fails the cookies stay
    and the response is 503.

    Args:
        request: FastAPI request (used to read the auth cookies).
        response: FastAPI response (used to clear auth cookies).
        body: Optional refresh token for Bearer-flow clients, which hold it
            outside a cookie.
        current_user: Current authenticated user.
        authorization: Authorization header (used to extract the JTI for
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
    revoked = True
    sids = set()
    if raw_token:
        ok, sid = await _revoke_token(raw_token, current_user, "access")
        revoked = ok and revoked
        sids.add(sid)

    # A captured refresh token must not mint new access tokens after logout.
    # The cookie and the body field can differ (a Bearer client next to a
    # browser session), so both are revoked.
    refresh_tokens = {
        request.cookies.get(REFRESH_COOKIE_NAME),
        body.refresh_token if body else None,
    }
    for raw in refresh_tokens - {None, ""}:
        ok, sid = await _revoke_token(raw, current_user, "refresh")
        revoked = ok and revoked
        sids.add(sid)

    # Refresh refuses an ended sign-in, so end it even when the blacklist
    # write did not happen.
    ended = True
    for sid in sids - {None}:
        try:
            await asyncio.to_thread(sign_ins.end, sid, sign_ins.SIGN_OUT)
        except Exception as exc:
            logger.error("Failed to end sign-in for %s: %s", current_user.username, exc)
            ended = False

    # A token issued before sign-ins carries no sid. Nothing but the blacklist
    # revokes it, so a missed write must not be reported as success.
    if not ended or (not revoked and not (sids - {None})):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not revoke this session",
        )

    clear_auth_cookies(response)

    logger.info(f"User logged out: {current_user.username}")
    return {"message": "Logged out successfully"}


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
                await asyncio.to_thread(sign_ins.end, expired["sid"], sign_ins.EXPIRY)
            except Exception as exc:
                logger.error("Failed to end an expired sign-in: %s", exc)
    if not payload or payload.get("token_type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_REFRESH_TOKEN,
        )

    if await is_token_revoked(payload):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has been revoked",
        )

    user = await asyncio.to_thread(_get_user, session, payload["user_id"])
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    if issued_before_cutoff(payload, user.tokens_valid_after):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has been revoked",
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

    # A token minted before sign-ins existed carries no sid and starts none.
    sid = payload.get("sid")
    sign_in = None
    if sid:
        sign_in = await asyncio.to_thread(_open_sign_in, session, sid, user)
        if sign_in is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Sign-in has ended",
            )

    # Refresh tokens rotate: each one mints a single successor.
    if not payload.get("jti") or not await consume_refresh_jti(
        payload["jti"], _exp_datetime(payload.get("exp"))
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has already been used",
        )

    # Generate new tokens: the same sign-in, new jtis.
    _ua = request.headers.get("user-agent")
    access_token = AuthService.generate_jwt_token(
        user, "access", user_agent=_ua, sid=sid
    )
    refresh_token = AuthService.generate_jwt_token(
        user, "refresh", user_agent=_ua, sid=sid
    )

    refresh_payload = AuthService.verify_jwt_token(refresh_token) or {}
    if sign_in is not None:
        sign_in.expires_at = _exp_datetime(refresh_payload.get("exp"))
    if not service_account:
        _set_cookies_for(response, access_token, refresh_token)

    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=await asyncio.to_thread(_user_payload, user, session),
    )


@router.get("/me")
def get_current_user_info(
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: UnitOfWorkSession,
):
    """
    Get current user information.

    Args:
        current_user: Current authenticated user
        session: Database session

    Returns:
        User information with permissions
    """
    return _user_payload(current_user, session)


def _apply_profile_update(
    session: Session,
    user: User,
    full_name: Optional[str],
    email: Optional[str],
    current_password: Optional[str],
) -> tuple[bool, dict]:
    """Apply a profile edit; return whether the email changed and the new payload.

    The mailbox is checked before any column is written. A rejected email
    change must not depend on the request transaction rolling the name back.
    """
    changing_email = bool(email) and email.casefold() != user.email.casefold()
    if changing_email:
        # Password reset is mailed to the address on the account, so a live
        # session is not enough to point that address somewhere else.
        # 400, not 401: the session is valid and a body field is wrong. A 401
        # would make the web client refresh and retry, spending two rate-limit slots.
        if not current_password or not AuthService.verify_password(
            current_password, user.password_hash
        ):
            logger.warning(
                "Rejected email change for %s: current password %s",
                user.username,
                "missing" if not current_password else "incorrect",
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Current password is incorrect"
                    if current_password
                    else "Current password is required to change the email address"
                ),
            )
        existing = (
            session.query(User)
            .filter(User.email == email, User.user_id != user.user_id)
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already in use",
            )

    if full_name:
        user.full_name = full_name
    if changing_email:
        user.email = email
        user.is_verified = False

    # Flush so the read-back sees server defaults; the request's unit
    # of work commits.
    session.flush()
    session.refresh(user)
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

    try:
        email_changed, payload = await asyncio.to_thread(
            _apply_profile_update,
            session,
            current_user,
            body.full_name,
            body.email,
            body.current_password,
        )

        if email_changed:
            try:
                await revoke_all_for_user(current_user.user_id)
            except Exception as exc:
                logger.error(
                    "Email changed for %s; session cutoff is stored but the "
                    "Redis copy failed: %s",
                    current_user.username,
                    exc,
                )
            # After the Redis round-trip. A refresh during that wait must
            # still be older than the cutoff this request commits.
            stamp_session_cutoff(current_user)

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
    # bcrypt and the strength check are CPU-bound; only the Redis revoke awaits.
    if not await asyncio.to_thread(
        AuthService.verify_password, body.current_password, current_user.password_hash
    ):
        # 400, not 401: the session is valid, a body field is wrong. A 401 would
        # make the web client refresh and retry, spending two rate-limit slots.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )

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
        # Enforce no-reuse + hash + history rotation
        await asyncio.to_thread(_apply_new_password, current_user, body.new_password)

        # Invalidate every outstanding token for this user. The current
        # session is effectively logged out; the client should re-login.
        try:
            await revoke_all_for_user(current_user.user_id)
        except Exception as exc:
            logger.error(
                "Password changed for %s; session cutoff is stored but the "
                "Redis copy failed: %s",
                current_user.username,
                exc,
            )
        # After the Redis round-trip, so a refresh during that wait is still
        # older than the cutoff this request commits.
        stamp_session_cutoff(current_user)

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


@router.post("/mfa/setup", response_model=MFASetupResponse)
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
    _reject_if_locked(current_user)
    codes = AuthService.enable_mfa(current_user.user_id, body.code, session)

    if codes is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid MFA code"
        )

    return RecoveryCodesResponse(
        recovery_codes=codes, message="MFA enabled successfully"
    )


@router.post("/mfa/recovery-codes", response_model=RecoveryCodesResponse)
@limiter.limit("5/minute", key_func=session_rate_key)
def regenerate_recovery_codes(
    request: Request,
    current_user: Annotated[User, Depends(get_current_active_user)],
    session: UnitOfWorkSession,
):
    """
    Generate a fresh set of one-time MFA recovery codes, invalidating any
    previous codes. Requires MFA to already be set up.

    Returns:
        The new plaintext recovery codes, shown only once.
    """
    codes = AuthService.get_mfa_recovery_codes(current_user.user_id, session)
    if codes is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA is not set up for this user",
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
    _reject_if_locked(current_user)
    if not AuthService.disable_mfa(current_user.user_id, body.code, session):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid MFA code or MFA is not enabled",
        )

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
    if user and user.is_active and not _is_service_account(user):
        token = generate_reset_token(user.user_id)
        frontend_base = settings.vigil_frontend_url
        if frontend_base:
            link = reset_link(frontend_base, token)
        else:
            # Fall back to a raw token so a dev console backend still shows
            # something actionable when VIGIL_FRONTEND_URL isn't set.
            link = f"(token) {token}"
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
    Complete a password reset. Validates the signed token, enforces the
    password strength policy + reuse check, rotates the hash, and
    revokes every outstanding token so any existing sessions die.
    """
    user_id = await verify_reset_token(body.token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_INVALID_RESET_TOKEN,
        )

    user = await asyncio.to_thread(_get_user, session, user_id)
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
        user.failed_login_count = 0
        user.locked_until = None

        try:
            await revoke_all_for_user(user.user_id)
        except Exception as exc:
            logger.error(
                "Password reset for %s; session cutoff is stored but the "
                "Redis copy failed: %s",
                user.username,
                exc,
            )
        # After the Redis round-trip, so a refresh during that wait is still
        # older than the cutoff this request commits.
        stamp_session_cutoff(user)

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
