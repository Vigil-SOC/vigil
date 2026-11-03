"""Current-user resolution — the ``get_current_user`` FastAPI dependency.

Lives in ``core`` because it is auth resolution built entirely on ``core.auth``
(no deployable-specific code), so both the deployable routers under ``services``
and the versioned contract routers under ``core.api.v1`` may depend on it
without a ``core -> services`` layering violation.

``services.api.middleware.auth`` re-exports these names, so existing importers
are unaffected.
"""

import logging
from typing import Callable, Optional

from fastapi import Header, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from core.auth.auth_cookies import ACCESS_COOKIE_NAME
from core.auth.auth_service import AuthService
from core.auth.mcp_credential_service import looks_like_mcp_token
from core.auth.session_cutoff import issued_before_cutoff
from core.auth.token_blacklist import is_token_revoked
from core.cases.agent_closure import clear_http_actor, note_http_actor
from core.config import get_settings
from core.storage.models import User
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

ACCESS_TOKEN_TYPE = "access"

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


async def get_current_user(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> User:
    """
    Resolve the authenticated user from either the access_token HttpOnly
    cookie (browser flow) or an Authorization: Bearer header (API clients).

    Cookie is preferred when both are present.

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
    request.state.vigil_http_actor = None

    # DEV MODE: Bypass authentication and return mock user
    if DEV_MODE:
        logger.debug("DEV_MODE: Bypassing authentication")
        return await run_in_threadpool(_detached, _get_dev_user)

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

    # Reject revoked tokens (logout / password change / role change).
    if await is_token_revoked(payload):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Validate session fingerprint (user-agent binding).
    if not AuthService.verify_session_fingerprint(
        payload,
        user_agent=request.headers.get("user-agent"),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session fingerprint mismatch",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Get user from database
    user = await run_in_threadpool(
        _detached,
        lambda db: db.query(User).filter(User.user_id == payload["user_id"]).first(),
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive",
        )

    # Redis may have refused the cutoff write, or be serving reads without it.
    # The user row is the record of that sign-out.
    if issued_before_cutoff(payload, user.tokens_valid_after):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
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
