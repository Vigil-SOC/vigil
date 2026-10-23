"""Role-permission gates for routes and tools.

``Auth.REQUIRED`` only proves a login. What the login's role may do is the
``roles.permissions`` grant, checked here. Lives in ``core`` beside
``current_user`` so contract routers under ``core.api.v1`` can use it without a
``core -> services`` import.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from fastapi import Depends, Header, HTTPException, Request, status

from core.auth import current_user as current_user_module
from core.auth.auth_cookies import ACCESS_COOKIE_NAME
from core.auth.auth_service import AuthService
from core.auth.current_user import get_current_user
from core.auth.mcp_credential_service import looks_like_mcp_token
from core.integrations.mcp.surface import current_caller
from core.routing import UnitOfWorkSession
from core.storage.models import User
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

APPROVE_PERMISSION = "ai_decisions.approve"
# Medic, the install's health watcher: view it, and every write (feedback, pack,
# export). Seeded by 45_medic_permissions.sql; pinned by test_medic_route_matrix.
MEDIC_READ_PERMISSION = "medic.read"
MEDIC_ADMIN_PERMISSION = "medic.admin"

# The permission vocabulary (#2915). ``permissions`` is each name's origin.
# ``builtins`` is what an unmodified built-in holds once 0008 has run.
# A revision must not import this: its grants are frozen literals.
# users.*, findings.*, cases.read/write/delete, audit.*, medic.*, loglm.view,
# ai_chat.use, ai_decisions.approve and integrations.write stay as they are
# and are not re-originated. settings.read and settings.write stay on the
# role JSON until #2916 moves the last gate off them.
_SIGNED_IN_READ = "signed-in read"
_NEW_CAPABILITY = "new capability"


def _split_from(parent: str) -> str:
    return f"split from {parent}"


CATALOG = {
    "permissions": {
        "detections.read": _split_from("settings.read"),
        "detections.write": _split_from("settings.write"),
        "automation.read": _split_from("settings.read"),
        "automation.write": _split_from("settings.write"),
        "ai_autonomy.read": _split_from("settings.read"),
        "ai_autonomy.write": _split_from("settings.write"),
        "ai_autonomy.stop": _split_from("settings.write"),
        "platform.read": _split_from("settings.read"),
        "platform.write": _split_from("settings.write"),
        "integrations.read": _SIGNED_IN_READ,
        "integrations.install": _split_from("integrations.write"),
        "cases.assign": _split_from("cases.write"),
        "roles.manage": _NEW_CAPABILITY,
        "ai_decisions.read": _SIGNED_IN_READ,
        "settings.read": _SIGNED_IN_READ,
    },
    "builtins": {
        "role-admin": {
            "ai_autonomy.read": True,
            "ai_autonomy.stop": True,
            "ai_autonomy.write": True,
            "ai_chat.use": True,
            "ai_decisions.approve": True,
            "ai_decisions.read": True,
            "audit.forward": True,
            "audit.manage": True,
            "audit.read": True,
            "automation.read": True,
            "automation.write": True,
            "cases.assign": True,
            "cases.delete": True,
            "cases.read": True,
            "cases.write": True,
            "detections.read": True,
            "detections.write": True,
            "findings.delete": True,
            "findings.read": True,
            "findings.write": True,
            "integrations.install": True,
            "integrations.read": True,
            "integrations.write": True,
            "loglm.view": True,
            "medic.admin": True,
            "medic.read": True,
            "platform.read": True,
            "platform.write": True,
            "roles.manage": True,
            "settings.read": True,
            "settings.write": True,
            "users.delete": True,
            "users.read": True,
            "users.write": True,
        },
        "role-analyst": {
            "ai_autonomy.read": True,
            "ai_chat.use": True,
            "ai_decisions.approve": False,
            "ai_decisions.read": True,
            "automation.read": True,
            "cases.assign": True,
            "cases.delete": False,
            "cases.read": True,
            "cases.write": True,
            "detections.read": True,
            "findings.delete": False,
            "findings.read": True,
            "findings.write": True,
            "integrations.read": True,
            "integrations.write": False,
            "loglm.view": True,
            "platform.read": True,
            "settings.read": True,
            "settings.write": False,
            "users.read": False,
        },
        "role-manager": {
            "ai_autonomy.read": True,
            "ai_autonomy.stop": True,
            "ai_autonomy.write": True,
            "ai_chat.use": True,
            "ai_decisions.approve": True,
            "ai_decisions.read": True,
            "audit.read": True,
            "automation.read": True,
            "automation.write": True,
            "cases.assign": True,
            "cases.delete": True,
            "cases.read": True,
            "cases.write": True,
            "detections.read": True,
            "detections.write": True,
            "findings.delete": True,
            "findings.read": True,
            "findings.write": True,
            "integrations.install": True,
            "integrations.read": True,
            "integrations.write": True,
            "loglm.view": True,
            "medic.read": True,
            "platform.read": True,
            "platform.write": True,
            "settings.read": True,
            "settings.write": True,
            "users.delete": False,
            "users.read": True,
            "users.write": True,
        },
        "role-senior-analyst": {
            "ai_autonomy.read": True,
            "ai_autonomy.stop": True,
            "ai_chat.use": True,
            "ai_decisions.approve": True,
            "ai_decisions.read": True,
            "automation.read": True,
            "cases.assign": True,
            "cases.delete": False,
            "cases.read": True,
            "cases.write": True,
            "detections.read": True,
            "findings.delete": True,
            "findings.read": True,
            "findings.write": True,
            "integrations.install": True,
            "integrations.read": True,
            "integrations.write": True,
            "loglm.view": True,
            "platform.read": True,
            "settings.read": True,
            "settings.write": False,
            "users.read": True,
        },
        "role-viewer": {
            "ai_autonomy.read": True,
            "ai_chat.use": False,
            "ai_decisions.approve": False,
            "ai_decisions.read": True,
            "automation.read": True,
            "cases.read": True,
            "detections.read": True,
            "findings.read": True,
            "integrations.read": True,
            "platform.read": True,
            "settings.read": True,
            "users.read": False,
        },
    },
}

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# A credential is refused before the role check (decision 9). 403, including
# in dev mode, where the session dependency would otherwise accept it.
MCP_CREDENTIAL_DENIED = "An MCP credential cannot do this"
SIGN_IN_REQUIRED = "A person sign-in is required"


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


def _presented_token(request: Request, authorization: Optional[str]) -> Optional[str]:
    token = request.cookies.get(ACCESS_COOKIE_NAME)
    if token:
        return token
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


def reject_mcp_credential(request: Request, authorization: Optional[str]) -> None:
    """403 before a permission check when the caller is a credential.

    ``current_caller`` is set for the duration of an MCP tool call. A bearer
    that looks like a credential is the same refusal on the HTTP routes.
    """
    token = _presented_token(request, authorization)
    if current_caller() is not None or (token and looks_like_mcp_token(token)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=MCP_CREDENTIAL_DENIED
        )


async def resolve_signed_in_user(
    request: Request,
    session: UnitOfWorkSession,
    authorization: Optional[str] = Header(None),
) -> User:
    """The session user, after a credential has already been refused."""
    reject_mcp_credential(request, authorization)
    user = await current_user_module.get_current_user(
        request, authorization, session=session
    )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="User account is inactive"
        )
    return user


def require_person_permission(permission: str) -> Callable[..., User]:
    """``permission`` held by a person principal, never by a credential.

    The kind check is ``AuthService.check_permission``, which refuses a
    non-person for ``audit.forward`` and ``audit.manage`` even when dev mode
    grants every role permission. A missing sign-in is refused first, except
    in dev mode, which has no token. The role matrix reads ``permission``.
    """

    def _check(request: Request, user: User = Depends(resolve_signed_in_user)) -> User:
        if not current_user_module.DEV_MODE:
            claims = getattr(request.state, "access_claims", None)
            if not isinstance(claims, dict) or not claims.get("sid"):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail=SIGN_IN_REQUIRED
                )
        if not AuthService.check_permission(user.user_id, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: {permission} required",
            )
        return user

    _check.permission, _check.unsafe_only = permission, False
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
    call. No bound principal is a system run — one the orchestrator or a
    handoff started — which is not a role to consult. A person who started a
    run is named on it, and this check is theirs.
    """
    caller = current_caller()
    if caller is None or username_has_permission(caller, permission):
        return None
    logger.warning("%s refused: %s required", caller, permission)
    return {"error": f"{caller} may not do this: {permission} required"}
