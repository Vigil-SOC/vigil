"""Role-permission gates for routes and tools.

``Auth.REQUIRED`` only proves a login. What the login's role may do is the
``roles.permissions`` grant, checked here. Lives in ``core`` beside
``current_user`` so contract routers under ``core.api.v1`` can use it without a
``core -> services`` import.
"""

from __future__ import annotations

import functools
import importlib.util
import logging
from pathlib import Path
from typing import Callable, Optional

from fastapi import Depends, Header, HTTPException, Request, status

from core.auth import current_user as current_user_module
from core.auth.auth_cookies import ACCESS_COOKIE_NAME
from core.auth.auth_service import AuthService
from core.auth.current_user import get_current_user
from core.auth.mcp_credential_service import looks_like_mcp_token
from core.auth.tool_principal import current_claims
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

# Every key a role may hold: Admin holds them all.
KNOWN_PERMISSIONS = frozenset(CATALOG["permissions"]) | frozenset(
    CATALOG["builtins"]["role-admin"]
)
# Platform-equivalent: installing integration code runs code as Vigil. In no preset.
PLATFORM_EQUIVALENT = frozenset({"integrations.install"})


def _without(base: dict, *keys: str) -> dict:
    return {k: v for k, v in base.items() if k not in keys}


# Decision 4 of #2851. "Start from…" choices: copying one writes an ordinary
# custom role, so a later change here never reaches a copy.
PRESETS = {
    "soc-lead": {
        "name": "SOC Lead",
        "description": "Manager without platform powers.",
        "permissions": _without(
            CATALOG["builtins"]["role-manager"],
            "platform.write",
            "settings.write",
            "integrations.install",
        ),
    },
    "detection-engineer": {
        "name": "Detection Engineer",
        "description": "Analyst plus detections and automation.",
        "permissions": {
            **CATALOG["builtins"]["role-analyst"],
            "detections.write": True,
            "automation.write": True,
        },
    },
    "auditor": {
        "name": "Auditor",
        "description": "Reads everything, including the audit trail. No writes.",
        "permissions": {
            k: True
            for k in sorted(KNOWN_PERMISSIONS)
            if k.endswith(".read") or k == "loglm.view"
        },
    },
}


@functools.cache
def _catalog_diff() -> Callable[[dict, dict], dict]:
    # The revision's own key diff; ``versions`` is not a package.
    path = Path(__file__).parents[1] / "upgrade/versions/0008_permission_catalog.py"
    spec = importlib.util.spec_from_file_location("permission_catalog_0008", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._diff


def builtin_diff(role_id: str, permissions: dict) -> dict:
    """How a built-in row differs from the catalog, per key. Empty when it doesn't."""
    return _catalog_diff()(dict(permissions or {}), CATALOG["builtins"][role_id])


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# The revision that copies successor grants onto roles (#2915). While it is
# missing from alembic_version, a gate split from a live write keeps that
# write, and a gate this revision introduces stays open.
CATALOG_REVISION = "0008_permission_catalog"
_WRITE_PARENTS = frozenset({"settings.write", "integrations.write"})
_SPLIT_PREFIX = "split from "


def catalog_revision_applied(session) -> bool:
    """Whether ``session``'s database includes ``0008_permission_catalog``.

    The same walk as ``trail_ready``: the table, then each stamped revision's
    ancestors. A database that cannot be opened is handled by the caller as
    not yet upgraded, and so is one that is not Postgres (a SQLite test
    database has no migration chain). A read error once it is open raises, so
    a transient failure cannot read the revision as absent and open the split
    gates. Not a setting, and not a call to ``trail_ready``.
    """
    from alembic.util import CommandError
    from sqlalchemy import text

    from core.upgrade.command import script_directory

    if session.get_bind().dialect.name != "postgresql":
        return False
    present = session.execute(
        text("SELECT to_regclass('public.alembic_version')")
    ).scalar()
    if present is None:
        return False
    script = script_directory()
    rows = session.execute(text("SELECT version_num FROM alembic_version"))
    for (version,) in rows:
        try:
            if any(
                revision.revision == CATALOG_REVISION
                for revision in script.iterate_revisions(version, "base")
            ):
                return True
        except CommandError:  # newer than this image: the upgrade refuses it too
            continue
    return False


def enforced_permission(
    permission: str,
    session,
    *,
    introduced: bool = False,
    before_revision: Optional[str] = None,
) -> Optional[str]:
    """The name to check, or None when the gate stays open.

    With the catalog revision applied, the declared name is the check. Without
    it, a successor of ``settings.write`` or ``integrations.write`` keeps that
    parent, and every other catalog permission is new and stays open. A name
    the catalog does not re-origin (the live gates, and ``{integration}.view``)
    is checked either way. ``introduced`` is a route that had no gate: it stays
    open until the revision even when its name is a write successor.
    ``before_revision`` is the live check a route keeps when its new name is
    itself a live permission (``integrations.write``), which the catalog does
    not record as a successor. ``session`` may be None when no database is
    open; that counts as the revision being absent unless the revision check
    is stubbed to say otherwise.
    """
    if session is not None and catalog_revision_applied(session):
        return permission
    if session is None:
        try:
            if catalog_revision_applied(None):  # type: ignore[arg-type]
                return permission
        except Exception:
            pass
    if introduced:
        return None
    if before_revision is not None:
        return before_revision
    origin = CATALOG["permissions"].get(permission)
    if isinstance(origin, str) and origin.startswith(_SPLIT_PREFIX):
        parent = origin[len(_SPLIT_PREFIX) :]
        if parent in _WRITE_PARENTS:
            return parent
    if origin is not None:
        return None
    return permission


def _missing(
    user_id: str,
    permission: str,
    session,
    *,
    introduced: bool = False,
    before_revision: Optional[str] = None,
) -> Optional[str]:
    enforced = enforced_permission(
        permission,
        session,
        introduced=introduced,
        before_revision=before_revision,
    )
    if enforced is None:
        return None
    if AuthService.check_permission(user_id, enforced, session=session):
        return None
    return enforced


def missing_permission(
    user_id: str,
    permission: str,
    session=None,
    *,
    introduced: bool = False,
    before_revision: Optional[str] = None,
) -> Optional[str]:
    """The permission a refusal should name, or None when the call is allowed.

    No database yet (a unit test, or a process that has not connected) is the
    same as the revision being absent: today's gates, and a stubbed
    ``check_permission`` still runs.
    """
    if session is not None:
        return _missing(
            user_id,
            permission,
            session,
            introduced=introduced,
            before_revision=before_revision,
        )
    # A database this process cannot open is the same as the revision being
    # absent. Unit tests point Postgres at an unreachable host. Only the open
    # falls back: an error once the database is open is raised, so a transient
    # failure cannot read the revision as absent and open the split gates.
    from contextlib import ExitStack

    from sqlalchemy.exc import SQLAlchemyError

    with ExitStack() as stack:
        try:
            owned = stack.enter_context(unit_of_work())
            # A session connects on first use: connect here, so a host that
            # cannot be reached falls back and a later read error raises.
            owned.connection()
        except (RuntimeError, SQLAlchemyError):
            return _missing(
                user_id,
                permission,
                None,
                introduced=introduced,
                before_revision=before_revision,
            )
        return _missing(
            user_id,
            permission,
            owned,
            introduced=introduced,
            before_revision=before_revision,
        )


def deny_unless(
    user_id: str,
    permission: str,
    session=None,
    *,
    introduced: bool = False,
    before_revision: Optional[str] = None,
) -> None:
    """403 naming the permission the check actually used."""
    missing = missing_permission(
        user_id,
        permission,
        session,
        introduced=introduced,
        before_revision=before_revision,
    )
    if missing is None:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Permission denied: {missing} required",
    )


# A credential is refused before the role check (decision 9). 403, including
# in dev mode, where the session dependency would otherwise accept it.
MCP_CREDENTIAL_DENIED = "An MCP credential cannot do this"
SIGN_IN_REQUIRED = "A person sign-in is required"


def require_permission(
    permission: str,
    *,
    unsafe_only: bool = False,
    introduced: bool = False,
    before_revision: Optional[str] = None,
) -> Callable[..., User]:
    """Dependency that answers 403 unless the signed-in user holds ``permission``.

    ``unsafe_only`` skips GET/HEAD/OPTIONS, for a router-level gate that guards
    the writes of a router whose reads stay open to every role. ``introduced``
    and ``before_revision`` select the pre-revision check; see
    ``enforced_permission``.
    """

    def _check(request: Request, user: User = Depends(get_current_user)) -> User:
        if unsafe_only and request.method in _SAFE_METHODS:
            return user
        # Omit the default keywords so a test stub of the two-argument
        # call still matches a gate that is not one of the two exceptions.
        extra = {}
        if introduced:
            extra["introduced"] = True
        if before_revision is not None:
            extra["before_revision"] = before_revision
        deny_unless(user.user_id, permission, **extra)
        return user

    # Introspectable by the role x route matrix test.
    _check.permission, _check.unsafe_only = permission, unsafe_only
    _check.introduced, _check.before_revision = introduced, before_revision
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
        deny_unless(user.user_id, permission)
        return user

    _check.permission, _check.unsafe_only = permission, False
    return _check


def permission_gate(
    permission: str,
    *,
    unsafe_only: bool = False,
    introduced: bool = False,
    before_revision: Optional[str] = None,
) -> Depends:
    """``Depends(require_permission(...))``, ready for ``dependencies=[...]``."""
    return Depends(
        require_permission(
            permission,
            unsafe_only=unsafe_only,
            introduced=introduced,
            before_revision=before_revision,
        )
    )


def username_has_permission(username: str, permission: str) -> bool:
    """Whether the account named ``username`` holds ``permission``.

    For paths that carry only a username (chat and MCP tool calls). An unknown
    account holds nothing, except under DEV_MODE, which holds built-in Admin.
    """
    with unit_of_work() as session:
        user = session.query(User).filter(User.username == username).first()
        return (
            missing_permission(user.user_id if user else "", permission, session)
            is None
        )


def permission_refusal(permission: str) -> Optional[dict[str, str]]:
    """An error when the bound principal does not hold ``permission``.

    A credential has no permissions of its own. The person it names is checked
    against their live role, so a demotion or a role edit applies on the next
    call. A run no person started is bound by its signed run claims, not a
    role: the autonomy ceiling governs it at invoke. Nothing bound at all is
    refused (#3306).
    """
    caller = current_caller()
    if caller is None:
        if current_claims() is not None:
            return None
        logger.warning("unbound caller refused: %s required", permission)
        return {"error": f"no principal is bound: {permission} required"}
    if username_has_permission(caller, permission):
        return None
    logger.warning("%s refused: %s required", caller, permission)
    return {"error": f"{caller} may not do this: {permission} required"}
