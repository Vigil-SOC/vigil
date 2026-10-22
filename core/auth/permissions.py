"""Role-permission gates for routes and tools.

``Auth.REQUIRED`` only proves a login. What the login's role may do is the
``roles.permissions`` grant, checked here. Lives in ``core`` beside
``current_user`` so contract routers under ``core.api.v1`` can use it without a
``core -> services`` import.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from fastapi import Depends, HTTPException, Request, status

from core.auth.auth_service import AuthService
from core.auth.current_user import get_current_user
from core.integrations.mcp.surface import current_caller
from core.storage.models import User
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

APPROVE_PERMISSION = "ai_decisions.approve"

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def require_permission(
    permission: str, *, unsafe_only: bool = False
) -> Callable[..., User]:
    """Dependency that answers 403 unless the signed-in user holds ``permission``.

    ``unsafe_only`` skips GET/HEAD/OPTIONS, for a router-level gate that guards
    the writes of a router whose reads stay open to every role.
    """

    def _check(request: Request, user: User = Depends(get_current_user)) -> User:
        if unsafe_only and request.method in _SAFE_METHODS:
            return user
        if not AuthService.check_permission(user.user_id, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: {permission} required",
            )
        return user

    # Introspectable by the role x route matrix test.
    _check.permission, _check.unsafe_only = permission, unsafe_only
    return _check


def permission_gate(permission: str, *, unsafe_only: bool = False) -> Depends:
    """``Depends(require_permission(...))``, ready for ``dependencies=[...]``."""
    return Depends(require_permission(permission, unsafe_only=unsafe_only))


def username_has_permission(username: str, permission: str) -> bool:
    """Whether the account named ``username`` holds ``permission``.

    For paths that carry only a username (chat and MCP tool calls).
    """
    with unit_of_work() as session:
        user = session.query(User).filter(User.username == username).first()
        user_id = user.user_id if user else ""
    # An unknown id holds nothing, except under DEV_MODE, which grants all.
    return AuthService.check_permission(user_id, permission)


def permission_refusal(permission: str) -> Optional[dict[str, str]]:
    """An error when the bound principal does not hold ``permission``.

    A credential has no permissions of its own. The person it names is checked
    against their live role, so a demotion or a role edit applies on the next
    call. No bound principal is a hunt, which is not a role to consult.
    """
    caller = current_caller()
    if caller is None or username_has_permission(caller, permission):
        return None
    logger.warning("%s refused: %s required", caller, permission)
    return {"error": f"{caller} may not do this: {permission} required"}
