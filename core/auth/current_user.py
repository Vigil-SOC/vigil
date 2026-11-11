"""Current-user resolution — the ``get_current_user`` FastAPI dependency.

Lives in ``core`` because it is auth resolution built entirely on ``core.auth``
(no deployable-specific code), so both the deployable routers under ``services``
and the versioned contract routers under ``core.api.v1`` may depend on it
without a ``core -> services`` layering violation.

``services.api.middleware.auth`` re-exports these names, so existing importers
are unaffected.
"""

import logging
from contextvars import ContextVar
from typing import Any, Callable, Optional

from fastapi import Header, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from core.auth import security_keys, session_policy, sign_ins
from core.auth.auth_cookies import ACCESS_COOKIE_NAME
from core.auth.auth_service import AuthService
from core.auth.mcp_credential_service import (
    authenticate,
    looks_like_api_token,
    looks_like_mcp_token,
)
from core.cases.agent_closure import clear_http_actor, note_http_actor
from core.config import get_settings
from core.storage.models import Role, SignIn, User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)

# The request ``get_current_user`` is authenticating, for the rest of that request.
current_request: ContextVar[Optional[Request]] = ContextVar(
    "vigil_current_request", default=None
)

ACCESS_TOKEN_TYPE = "access"

# Set by the console on a request a person's own input caused. Polling and
# streams do not carry it, so they never count as activity.
USER_ACTION_HEADER = "x-vigil-user-action"

_SIGN_IN_ENDED = "Sign-in has ended"
# A refusal is (status, detail, end reason); the reason is set only for an ended sign-in.
_Refusal = tuple[int, Any, Optional[str]]

# One answer for every way an API token can fail, so a probe learns nothing.
_BAD_TOKEN = "Invalid or expired token"

# The 403 detail.code of a covered account with no MFA, on every route but the setup ones.
MFA_SETUP_REQUIRED = "mfa_setup_required"


def mfa_setup_route(endpoint: Callable) -> Callable:
    """Mark a route the MFA requirement lets through, so a person can set MFA up.

    Put it directly under the route decorator: the route's own function is read.
    """
    endpoint.mfa_setup_route = True  # type: ignore[attr-defined]
    return endpoint


NEEDS_MFA = "Set up two-step sign-in to continue"
NEEDS_KEY = "Add a security key to continue"


def setup_needed(
    db: Session,
    user: User,
    held: list[str],
    current: session_policy.SessionPolicy,
    via_sso: bool = False,
) -> Optional[str]:
    """Why the requirement holds this account at setup, or None: it is covered and
    has no MFA, or it is covered by require_phishing_resistant, holds no key and
    keys are available (unavailable, the app still counts: a step-down). The one
    check behind the gate and the console's setup screen. A sign-in through single
    sign-on is never held: the provider carries the MFA."""
    if via_sso:
        return None
    machine = user.service_account is True
    if not user.mfa_enabled and session_policy.mfa_covered(
        held, machine, current.require_mfa
    ):
        return NEEDS_MFA
    if (
        session_policy.mfa_covered(held, machine, current.require_phishing_resistant)
        and not security_keys.has_keys(db, user.principal_id)
        and security_keys.availability().available
    ):
        return NEEDS_KEY
    return None


# Dev mode flag - ONLY for development, never in production!
DEV_MODE = get_settings().dev_mode

# Every gate the flag opens, so the announcement says exactly what is exposed.
# /internal is deliberately absent: core/agents/internal_auth.py is shared-secret
# only and has no dev-mode branch.
DEV_MODE_OPEN_GATES = (
    "session auth (every request runs as a mock admin with full permissions)",
    "vstrike inbound (POST /api/integrations/vstrike/findings accepts pushes "
    "without its bearer key)",
)

if DEV_MODE:
    logger.warning(
        "⚠️  DEV_MODE is ENABLED - authentication is BYPASSED. Gates opened: %s. "
        "This is for a developer's own machine only; set DEV_MODE=false in .env "
        "to require a login.",
        "; ".join(DEV_MODE_OPEN_GATES),
    )

# The synthetic stand-in, for a database with no admin row. Cached because it is
# never persisted: re-building it per request would hand out a new user_id each time.
_dev_user = None


def _get_dev_user(session: Session) -> User:
    """Resolve the admin the DEV_MODE bypass authenticates as.

    The real row is re-read every request: a cached ORM instance detaches when
    the session that loaded it closes, and reading a column off it then raises
    DetachedInstanceError. Only the transient fallback below is held.
    """
    global _dev_user

    admin = session.query(User).filter(User.username == "admin").first()
    if admin is not None:
        return admin

    # Transient and never added to the session, so its attributes cannot expire.
    if _dev_user is None:
        import uuid

        from core.storage.models import Role

        admin_role = session.query(Role).filter(Role.name == "admin").first()
        _dev_user = User(
            user_id=str(uuid.uuid4()),
            username="dev-user",
            email="dev@localhost",
            password_hash="",  # Not used in dev mode
            role_id=admin_role.role_id if admin_role else str(uuid.uuid4()),
            is_active=True,
            mfa_enabled=False,
        )
        logger.info("Created mock dev user (not persisted to DB)")

    return _dev_user


def _detached(lookup: Callable[[Session], Optional[User]]) -> Optional[User]:
    """Run ``lookup`` in a short session and detach its row.

    Holding the request's session for the lookup kept its connection idle in
    transaction for the whole handler (#3660). Callers run this in a worker
    thread: a checkout waiting on a full pool must not block the event loop.
    """
    with unit_of_work() as db:
        user = lookup(db)
        # The transient dev fallback was never in the session.
        if user is not None and user in db:
            db.expunge(user)
        return user


def _check_sign_in(
    user_id: str,
    sid: str,
    user_agent: Optional[str],
    own_action: bool,
    setup_route: bool = False,
) -> tuple[Optional[User], Optional[_Refusal]]:
    """Load the user with the sign-in row and judge the row, in one short session.

    Returns the detached user, or the refusal as (status, detail, end reason). A
    sign-in found past a limit is ended here, with its reason, and commits with the 401.
    An account the MFA requirement covers, with no MFA yet (or the key requirement,
    with no key), is refused with 403 ``mfa_setup_required`` unless the route is a setup route. Judged on every
    request, so a role edit or a raised setting applies at the next one.
    """
    with unit_of_work() as db:
        user = db.query(User).filter(User.user_id == user_id).first()
        if not user:
            return None, (status.HTTP_401_UNAUTHORIZED, "User not found", None)
        if not user.is_active:
            return None, (status.HTTP_403_FORBIDDEN, "User account is inactive", None)
        row = db.get(SignIn, sid)
        if (
            row is None
            or row.ended_at is not None
            or row.principal_id != user.principal_id
        ):
            ended = row.end_reason if row is not None else None
            return None, (status.HTTP_401_UNAUTHORIZED, _SIGN_IN_ENDED, ended)
        # The browser the sign-in began in. A mismatch refuses this request and
        # ends nothing: the real browser still holds the same tokens.
        if row.user_agent and row.user_agent != (user_agent or ""):
            return None, (
                status.HTTP_401_UNAUTHORIZED,
                "Session fingerprint mismatch",
                None,
            )

        machine = user.service_account is True
        role = db.get(Role, user.role_id)
        held = [name for name, on in ((role and role.permissions) or {}).items() if on]
        current = session_policy.policy()
        limit = session_policy.limits(held, machine, current)
        now = utcnow()
        over = None
        if row.started_at + limit.absolute <= now:
            over = sign_ins.LENGTH_LIMIT
        elif limit.idle and row.last_action_at + limit.idle <= now:
            over = sign_ins.IDLE
        if over:
            sign_ins.end(sid, over, db)
            return None, (status.HTTP_401_UNAUTHORIZED, _SIGN_IN_ENDED, over)

        needed = (
            None
            if setup_route
            else setup_needed(
                db, user, held, current, via_sso=row.auth_method == sign_ins.SSO
            )
        )
        if needed:
            return None, (
                status.HTTP_403_FORBIDDEN,
                {"code": MFA_SETUP_REQUIRED, "message": needed},
                None,
            )

        if own_action and not machine:
            sign_ins.stamp_action(sid, db)
        db.expunge(user)
        return user, None


def _bearer(authorization: Optional[str]) -> Optional[str]:
    """The token of a well-formed ``Authorization: Bearer`` header, else None."""
    parts = (authorization or "").split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


def is_token_request(request: Optional[Request] = None) -> bool:
    """Whether the request being served authenticated with an API token."""
    request = request or current_request.get()
    actor = getattr(getattr(request, "state", None), "vigil_http_actor", None)
    return actor is not None and actor.via == "token"


def is_machine_request(user: User, request: Optional[Request] = None) -> bool:
    """A service account, or anyone calling with an API token: a machine may only
    narrow access, never widen it."""
    machine = getattr(user, "service_account", False) is True
    return machine or is_token_request(request)


async def get_current_user(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> User:
    """
    Resolve the authenticated user from either the access_token HttpOnly
    cookie (browser flow) or an Authorization: Bearer header (API clients).

    Cookie is preferred when both are present, except for an API token
    (``Bearer vgl_api_...``): that is judged alone, never beside a cookie, and
    is a machine caller whoever holds it.

    In DEV_MODE, authentication is bypassed and a mock admin user is returned.

    Args:
        request: FastAPI request (used to read the access_token cookie).
        authorization: Authorization header (Bearer token fallback).

    Returns:
        Current User object, detached: the lookup's session closes before the
        endpoint runs, so a writer must load its own copy into its session.

    Raises:
        HTTPException: If no token is present, validation fails, or the user
            is not found (production only).
    """
    # A previous request on this context must not decide this one. Dev mode
    # is not a cookie, so it stays cleared: the bypass is an agent close.
    clear_http_actor()
    # For deny_unless, which has no request: a privileged write asks to confirm.
    current_request.set(request)
    request.state.vigil_http_actor = None

    # DEV MODE: Bypass authentication and return mock user
    if DEV_MODE:
        logger.debug("DEV_MODE: Bypassing authentication")
        return await run_in_threadpool(_detached, _get_dev_user)

    presented = _bearer(authorization)
    if presented and looks_like_api_token(presented):
        found = await run_in_threadpool(authenticate, presented)
        if found is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=_BAD_TOKEN,
                headers={"WWW-Authenticate": "Bearer"},
            )
        # No sign-in row and no claims: what needs a person's sign-in refuses it.
        request.state.vigil_http_actor = note_http_actor(
            username=found.user.username,
            principal_id=found.user.principal_id,
            via="token",
            person=False,
        )
        return found.user

    cookie = request.cookies.get(ACCESS_COOKIE_NAME)
    # The cookie wins when a bearer is also present.
    via = "cookie" if cookie else "bearer"
    token: Optional[str] = cookie

    if not token and authorization:
        # Bearer fallback for CLI / scripts / integrations that haven't
        # migrated to the cookie flow.
        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization header format",
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = parts[1]

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # An MCP credential is not a session. It says a program was given standing
    # access, not that a person signed in, and it opens the MCP surface alone.
    # Refused by name rather than as an invalid token, because the holder has a
    # working credential and needs to know it is working in the wrong place.
    if looks_like_mcp_token(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="An MCP credential does not authenticate this API; sign in instead",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Verify JWT token
    payload = AuthService.verify_jwt_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Only an access token is a session. A refresh token (or one with a missing
    # or unknown type) must not work as a bearer credential on protected routes.
    if payload.get("token_type") != ACCESS_TOKEN_TYPE:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # The sign-in row is the record: a token is good only while its row is open
    # and inside the session policy.
    sid = payload.get("sid")
    if not sid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in again",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # A script's bearer call is its person's own action; the console's cookie
    # requests count only when they carry the marker.
    own_action = via == "bearer" or request.headers.get(USER_ACTION_HEADER) is not None
    user, refusal = await run_in_threadpool(
        _check_sign_in,
        payload["user_id"],
        sid,
        request.headers.get("user-agent"),
        own_action,
        getattr(request.scope.get("endpoint"), "mfa_setup_route", False),
    )
    if refusal:
        raise HTTPException(
            status_code=refusal[0],
            detail=refusal[1],
            headers=(
                {
                    "WWW-Authenticate": "Bearer",
                    **({sign_ins.END_REASON_HEADER: refusal[2]} if refusal[2] else {}),
                }
                if refusal[0] == 401
                else None
            ),
        )

    # The audit poll reads this to require a person sign-in before the role check.
    request.state.access_claims = payload
    # The account flag is a bool on a real user. Anything else (a stand-in in a
    # test) is a person; only an explicit True is a machine login.
    machine = getattr(user, User.service_account.key, False)
    request.state.vigil_http_actor = note_http_actor(
        username=user.username,
        principal_id=getattr(user, "principal_id", None),
        via=via,
        person=machine is not True,
    )
    return user
