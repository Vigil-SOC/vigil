"""
Authentication Middleware - JWT validation and RBAC enforcement.

The active-user dependency and permission checks. Current-user resolution and
the DEV_MODE bypass live in ``core.auth.current_user`` (so contract routers can
depend on them without a ``core -> services`` import); ``get_current_user`` is
re-exported here for existing importers.
"""

from fastapi import Depends, HTTPException, status

from core.auth.auth_service import AuthService
from core.auth.current_user import get_current_user  # noqa: F401  (re-export)
from core.storage.models import User


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
