"""
Authentication Middleware - JWT validation and RBAC enforcement.

Provides middleware for FastAPI to validate JWT tokens and check permissions.
Supports DEV_MODE for bypassing authentication during development.
"""

import logging
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from core.auth.auth_cookies import ACCESS_COOKIE_NAME
from core.auth.auth_service import AuthService
from core.auth.token_blacklist import is_token_revoked
from core.config import get_settings
from core.routing import UnitOfWorkSession
from core.storage.models import User

logger = logging.getLogger(__name__)

# Dev mode flag - ONLY for development, never in production!
DEV_MODE = get_settings().dev_mode

if DEV_MODE:
    logger.warning("⚠️  DEV_MODE is ENABLED - Authentication is BYPASSED!")
    logger.warning("⚠️  This should NEVER be enabled in production!")

# Mock dev user for dev mode
_dev_user = None


def _get_dev_user(session: Session) -> User:
    """
    Get or create a mock dev user for development mode.

    Args:
        session: Database session

    Returns:
        Mock dev user with admin permissions
    """
    global _dev_user

    if _dev_user is None:
        # Try to get existing admin user
        _dev_user = session.query(User).filter(User.username == "admin").first()

        # If no admin, create a mock user object (won't be persisted)
        if _dev_user is None:
            import uuid

            from core.storage.models import Role

            # Try to get admin role
            admin_role = session.query(Role).filter(Role.name == "admin").first()

            # Create mock user
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


async def get_current_user(
    request: Request,
    authorization: Optional[str] = Header(None),
    *,
    session: UnitOfWorkSession,
) -> User:
    """
    Resolve the authenticated user from either the access_token HttpOnly
    cookie (browser flow) or an Authorization: Bearer header (API clients).

    Cookie is preferred when both are present.

    In DEV_MODE, authentication is bypassed and a mock admin user is returned.

    Args:
        request: FastAPI request (used to read the access_token cookie).
        authorization: Authorization header (Bearer token fallback).
        session: Database session.

    Returns:
        Current User object.

    Raises:
        HTTPException: If no token is present, validation fails, or the user
            is not found (production only).
    """
    # DEV MODE: Bypass authentication and return mock user
    if DEV_MODE:
        logger.debug("DEV_MODE: Bypassing authentication")
        return _get_dev_user(session)

    token: Optional[str] = request.cookies.get(ACCESS_COOKIE_NAME)

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

    # Verify JWT token
    payload = AuthService.verify_jwt_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
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
    user = session.query(User).filter(User.user_id == payload["user_id"]).first()
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

    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Dependency to get current active user.

    Args:
        current_user: Current user from get_current_user

    Returns:
        Current active User object
    """
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="User account is inactive"
        )
    return current_user


def _require_permission(current_user: User, permission: str) -> None:
    """Raise 403 unless the user holds ``permission``.

    A plain call, not a decorator: the callers are sync route handlers, so a
    decorator that awaits the endpoint would not work for them.
    """
    if not AuthService.check_permission(current_user.user_id, permission):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied: {permission} required",
        )


def require_settings_admin(current_user: User) -> None:
    """Raise 403 unless the user may change system settings."""
    _require_permission(current_user, "settings.write")


def require_integrations_admin(current_user: User) -> None:
    """Raise 403 unless the user may change integrations or MCP servers."""
    _require_permission(current_user, "integrations.write")
