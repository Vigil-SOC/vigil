"""
User Management API - Admin endpoints for managing users.

Handles user CRUD operations, role assignment, and user administration.
"""

import asyncio
import logging
import re
import uuid
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import func
from sqlalchemy.orm import Session

from core.auth import current_user as current_user_module
from core.auth import mcp_credential_service, principals, sign_ins, trail_events
from core.auth.auth_service import PERSON_ONLY, AuthService
from core.auth.password_validator import PasswordPolicyError, validate_password_strength
from core.auth.permissions import (
    CATALOG,
    KNOWN_PERMISSIONS,
    PLATFORM_EQUIVALENT,
    PRESETS,
    builtin_diff,
    deny_unless,
)
from core.auth.service_account import USERNAME_PATTERN as MEDIC_USERNAME_PATTERN
from core.auth.session_cutoff import stamp_session_cutoff
from core.auth.token_blacklist import revoke_all_for_user
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import Principal, Role, User
from core.storage.schemas import RoleSchema, UserSchema
from core.storage.unit_of_work import unit_of_work
from core.upgrade.adopt import LEGACY_ROLE_IDS
from services.api.middleware.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/users",
    tags=["users"],
    auth=Auth.REQUIRED,
)


# Request/Response Models
class CreateUserRequest(BaseModel):
    """Create user request."""

    username: str
    email: EmailStr
    password: str
    full_name: str
    role_id: str


class UpdateUserRequest(BaseModel):
    """Update user request."""

    full_name: Optional[str] = None
    email: Optional[EmailStr] = None
    role_id: Optional[str] = None
    is_active: Optional[bool] = None


class CreateRoleRequest(BaseModel):
    """Create a custom role. ``copy_from`` is a preset key or a role id."""

    name: str
    description: str = ""
    permissions: Optional[dict[str, bool]] = None
    copy_from: Optional[str] = None


class UpdateRoleRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    permissions: Optional[dict[str, bool]] = None


ROLES_MANAGE = "roles.manage"
BUILTIN_ROLE_REFUSED = (
    "Built-in roles can't be changed. Copy it to create a custom role."
)


def _is_service_account(user: User) -> bool:
    # Only an explicit True is one; a user object without the field is not.
    try:
        return user.service_account is True
    except AttributeError:
        return False


def _is_medic(user: User) -> bool:
    return _is_service_account(user) and bool(
        re.fullmatch(MEDIC_USERNAME_PATTERN, user.username or "")
    )


def _refuse_medic_reassignment(user: User) -> None:
    # Medic's gateway stays Viewer, an outside observer (#2851).
    if _is_medic(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is managed by Medic and stays Viewer",
        )


def _principal_kind(user: User, session: Session) -> Optional[str]:
    return (
        session.query(Principal.kind)
        .filter(Principal.principal_id == user.principal_id)
        .scalar()
    )


def _is_person_account(user: User, session: Session) -> bool:
    return not _is_service_account(user) and (
        _principal_kind(user, session) == principals.PERSON
    )


def _acts_as_person(user: User, session: Session) -> bool:
    """A person's own session. A service account's is not; DEV_MODE's dev user is."""
    if _is_service_account(user):
        return False
    kind = _principal_kind(user, session)
    return kind == principals.PERSON if kind else current_user_module.DEV_MODE


def _granted(permissions: Optional[dict]) -> set[str]:
    return {key for key, value in (permissions or {}).items() if value}


def _uncovered(current_user: User, permissions: Optional[dict], session) -> list[str]:
    held = AuthService.get_user_permissions(current_user.user_id, session)
    # A key the registry doesn't know has no effect, so it needs no cover.
    return sorted(
        key
        for key in _granted(permissions) & KNOWN_PERMISSIONS
        if not held.get(key, False)
    )


def _can_assign_role(current_user: User, target_role: Role, session: Session) -> bool:
    """Return True only if current_user holds every permission granted by target_role.

    Prevents a user with users.write from assigning a role that grants
    more privileges than they themselves have. The same comparison covers the
    account being edited: its current role is a privilege the caller must
    already hold before they may change or delete that account.
    """
    return not _uncovered(current_user, target_role.permissions, session)


def _refuse_uncovered(
    current_user: User, permissions: Optional[dict], session: Session, what: str
) -> None:
    missing = _uncovered(current_user, permissions, session)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{what}: you don't hold {', '.join(missing)}",
        )


def _refuse_machine_widening(
    current_user: User, session: Session, before: Optional[dict], after: dict
) -> None:
    """Widening anyone's access is a person's own act; a machine may narrow."""
    if _granted(after) - _granted(before) and not _acts_as_person(
        current_user, session
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Widening access needs a person's own sign-in; "
            "a service account may only narrow it",
        )


def _refuse_audit_read_to_machine(current_user: User, session: Session) -> None:
    if not AuthService.check_permission(current_user.user_id, "audit.forward", session):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: audit.forward required "
            "to give audit.read to a service account",
        )


def _role_managers(
    session: Session,
    roles: Optional[dict[str, Optional[dict]]] = None,
    users: Optional[dict[str, tuple[str, bool]]] = None,
) -> int:
    """Active people who can manage roles, with ``roles`` ({id: permissions})
    and ``users`` ({id: (role_id, is_active)}) applied as if saved."""
    held = {role.role_id: role.permissions for role in session.query(Role)}
    held.update(roles or {})
    people = (
        _not_deleted(session.query(User.user_id, User.role_id, User.is_active))
        .filter(Principal.kind == principals.PERSON)
        .all()
    )
    count = 0
    for user_id, role_id, active in people:
        role_id, active = (users or {}).get(user_id, (role_id, active))
        if active and (held.get(role_id) or {}).get(ROLES_MANAGE):
            count += 1
    return count


def _refuse_lockout(current_user: User, session: Session, **change) -> None:
    """A person may not leave nobody able to manage roles. A machine's
    narrowing is containment and stands; the roles list then says so."""
    if not _acts_as_person(current_user, session):
        return
    if _role_managers(session) and not _role_managers(session, **change):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"This would leave no active person holding {ROLES_MANAGE}",
        )


def _require_privileges_cover_target(
    current_user: User,
    target: User,
    session: Session,
    *,
    action: str,
) -> None:
    """Refuse ``action`` when ``target``'s role grants anything the caller lacks.

    A missing role grants nothing, so it does not block. Callers may act on
    their own account: they hold every permission their role grants.
    """
    role = session.query(Role).filter(Role.role_id == target.role_id).first()
    if role is not None and not _can_assign_role(current_user, role, session):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Cannot {action} a user whose role grants "
                "privileges you do not have"
            ),
        )


def _record_denial(exc: HTTPException, current_user: User, attempted: str) -> None:
    if exc.status_code == status.HTTP_403_FORBIDDEN:
        trail_events.refused(
            "auth.denied", actor=current_user.principal_id, attempted=attempted
        )


@contextmanager
def _denials(current_user: User, attempted: str):
    """Put a 403 from the block on the audit trail; it is still a 403."""
    try:
        yield
    except HTTPException as exc:
        _record_denial(exc, current_user, attempted)
        raise


@asynccontextmanager
async def _denials_async(current_user: User, attempted: str):
    """_denials for an async route: the write waits on the trail, off the loop."""
    try:
        yield
    except HTTPException as exc:
        await asyncio.to_thread(_record_denial, exc, current_user, attempted)
        raise


def _not_deleted(query):
    """Accounts whose principal is not deleted. A deleted one keeps its row
    for the record but is not managed through this API again."""
    return query.join(Principal, Principal.principal_id == User.principal_id).filter(
        Principal.deleted_at.is_(None)
    )


def _live_user(session: Session, user_id: str) -> User:
    user = _not_deleted(session.query(User)).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    return user


def _dump_accounts(session: Session, users: list[User]) -> list[dict]:
    """Each account, with what kind of principal it is and which of its
    role's permissions have no effect for it."""
    held = {role.role_id: role.permissions for role in session.query(Role)}
    dumped = []
    for user in users:
        entry = UserSchema.dump(user)
        machine = _is_service_account(user)
        entry["principal_kind"] = (
            principals.SERVICE_ACCOUNT if machine else principals.PERSON
        )
        entry["managed_by_medic"] = _is_medic(user)
        # Stored on the role. AuthService.check_permission refuses them for a
        # non-person, so for this account they do nothing.
        entry["no_effect"] = (
            sorted(PERSON_ONLY & _granted(held.get(user.role_id))) if machine else []
        )
        dumped.append(entry)
    return dumped


@router.get("/")
def list_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    role_id: Optional[str] = None,
    is_active: Optional[bool] = None,
    search: Optional[str] = None,
    *,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """
    List all users (requires users.read permission).

    Args:
        skip: Number of users to skip
        limit: Maximum number of users to return
        role_id: Filter by role ID
        is_active: Filter by active status
        search: Search in username, email, or full name
        current_user: Current authenticated user
        session: Database session

    Returns:
        List of users
    """
    # Check permission
    with _denials(current_user, "user.list"):
        deny_unless(current_user.user_id, "users.read")

    try:
        query = _not_deleted(session.query(User))

        # Apply filters
        if role_id:
            query = query.filter(User.role_id == role_id)

        if is_active is not None:
            query = query.filter(User.is_active == is_active)

        if search:
            search_pattern = f"%{search}%"
            query = query.filter(
                (User.username.ilike(search_pattern))
                | (User.email.ilike(search_pattern))
                | (User.full_name.ilike(search_pattern))
            )

        # Get total count
        total = query.count()

        # Apply pagination
        users = query.offset(skip).limit(limit).all()

        return {
            "total": total,
            "skip": skip,
            "limit": limit,
            "users": _dump_accounts(session, users),
        }

    except Exception as e:
        logger.error(f"List users error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list users",
        )


@router.get("/{user_id}")
def get_user(
    user_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """
    Get user by ID (requires users.read permission).

    Args:
        user_id: User ID
        current_user: Current authenticated user
        session: Database session

    Returns:
        User information
    """
    # Check permission (or allow users to view their own profile)
    if user_id != current_user.user_id:
        with _denials(current_user, "user.read"):
            deny_unless(current_user.user_id, "users.read")

    user = _live_user(session, user_id)
    user_dict = _dump_accounts(session, [user])[0]

    # Add role information
    role = session.query(Role).filter(Role.role_id == user.role_id).first()
    if role:
        user_dict["role"] = RoleSchema.dump(role)

    # Add permissions
    user_dict["permissions"] = AuthService.get_user_permissions(user_id)

    return user_dict


@router.post("/", status_code=status.HTTP_201_CREATED)
def create_user(
    request: CreateUserRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """
    Create a new user (requires users.write permission).

    Args:
        request: User creation details
        current_user: Current authenticated user
        session: Database session

    Returns:
        Created user information
    """
    with _denials(current_user, "user.create"):
        deny_unless(current_user.user_id, "users.write")
        return _create_user(request, current_user, session)


def _create_user(request: CreateUserRequest, current_user: User, session: Session):
    # Check permission
    deny_unless(current_user.user_id, "users.write")

    # Validate password against the full strength policy. Penalize passwords
    # built from the new account's own identifiers.
    try:
        validate_password_strength(
            request.password,
            user_inputs=[request.username, request.email],
        )
    except PasswordPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.as_detail(),
        )

    # Verify role exists
    role = session.query(Role).filter(Role.role_id == request.role_id).first()
    if not role:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role ID"
        )

    _refuse_uncovered(
        current_user, role.permissions, session, "Cannot assign this role"
    )
    # An account that holds a role is a widening.
    _refuse_machine_widening(current_user, session, None, role.permissions)

    # Create user
    user = AuthService.create_user(
        username=request.username,
        email=request.email,
        password=request.password,
        full_name=request.full_name,
        role_id=request.role_id,
        session=session,
    )

    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username or email already exists",
        )

    trail_events.record(
        session,
        "user.created",
        {"personal": trail_events.personal(user)},
        actor=current_user.principal_id,
        principal=user.principal_id,
        role_id=user.role_id,
    )
    logger.info(f"User created by {current_user.username}: {user.username}")
    return UserSchema.dump(user)


def _disable_alone(user_id: str) -> None:
    """Commit a disable on its own: it stands whatever else the PUT does."""
    with unit_of_work() as session:
        session.query(User).filter(User.user_id == user_id).update(
            {User.is_active: False}, synchronize_session=False
        )


def _apply_user_update(
    session: Session, current_user: User, user_id: str, request: UpdateUserRequest
) -> tuple[User, Optional[str], dict]:
    """Sync half of update_user. Returns the user, what made its existing
    tokens stale (an email or role change, else None), and the payload."""
    deny_unless(current_user.user_id, "users.write")

    user = _live_user(session, user_id)
    _require_privileges_cover_target(current_user, user, session, action="update")

    # Every check before any write: a disable commits ahead of the rest.
    if request.email is not None and request.email.casefold() != user.email.casefold():
        if _is_service_account(user):
            # A reachable address is the first step to a password reset.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="A service account's email can't be changed",
            )
        # Reset mail goes to the address on the account. The holder changes
        # it from PUT /api/auth/me, which asks for their current password.
        # Same rule for the holder: this route does not ask.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Change the email address from the signed-in account's own profile",
        )

    permission_change = None
    if request.role_id is not None:
        role = session.query(Role).filter(Role.role_id == request.role_id).first()
        if not role:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role ID"
            )
        _refuse_uncovered(
            current_user, role.permissions, session, "Cannot assign this role"
        )
        if role.role_id != user.role_id:
            _refuse_medic_reassignment(user)
            previous = session.query(Role).filter(Role.role_id == user.role_id).first()
            before = previous.permissions if previous else {}
            _refuse_machine_widening(current_user, session, before, role.permissions)
            if "audit.read" in _granted(role.permissions) - _granted(
                before
            ) and not _is_person_account(user, session):
                _refuse_audit_read_to_machine(current_user, session)
            permission_change = (
                sorted(_granted(before)),
                sorted(_granted(role.permissions)),
            )

    role_id = request.role_id if request.role_id is not None else user.role_id
    is_active = request.is_active if request.is_active is not None else user.is_active
    if is_active and not user.is_active:
        # Re-enabling restores the account's whole role: a widening.
        current = session.query(Role).filter(Role.role_id == role_id).first()
        _refuse_machine_widening(
            current_user, session, None, current.permissions if current else {}
        )
    if (role_id, is_active) != (user.role_id, user.is_active):
        _refuse_lockout(
            current_user, session, users={user.user_id: (role_id, is_active)}
        )

    actor, principal = current_user.principal_id, user.principal_id
    # A safety action: committed on its own and recorded, or held to record
    # late, so a trail failure on the rest of this PUT leaves it in place.
    if request.is_active is False and user.is_active and trail_events.ready(session):
        _disable_alone(user_id)
        trail_events.safety("user.disabled", actor=actor, principal=principal)
        session.refresh(user)

    entries = []
    stale_reason = None
    changed = []
    if request.full_name is not None:
        if request.full_name != user.full_name:
            changed.append("full_name")
        user.full_name = request.full_name

    if changed:
        entries.append(
            (
                "user.updated",
                {"personal": {k: getattr(user, k) for k in changed}},
                {"changed": changed},
            )
        )

    if request.role_id is not None:
        if request.role_id != user.role_id:
            stale_reason = stale_reason or "Role changed"
            entries.append(
                (
                    "user.role_changed",
                    None,
                    {
                        "role_id": request.role_id,
                        "previous_role_id": user.role_id,
                        "permissions_before": permission_change[0],
                        "permissions_after": permission_change[1],
                    },
                )
            )
        user.role_id = request.role_id

    if request.is_active is not None:
        if request.is_active and not user.is_active:
            entries.append(("user.enabled", None, {}))
        user.is_active = request.is_active

    # Flush so the read-back sees server defaults; the request's unit
    # of work commits, entries included: a trail failure rolls this back.
    session.flush()
    session.refresh(user)
    for action, beside, fields in entries:
        trail_events.record(
            session, action, beside, actor=actor, principal=principal, **fields
        )
    return user, stale_reason, UserSchema.dump(user)


@router.put("/{user_id}")
async def update_user(
    user_id: str,
    request: UpdateUserRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """
    Update user information (requires users.write permission).

    The caller's permissions must also cover the target account's current
    role. Holding users.write is not enough to edit an account whose role
    grants something the caller does not have.

    Args:
        user_id: User ID to update
        request: Update details
        current_user: Current authenticated user
        session: Database session

    Returns:
        Updated user information
    """
    try:
        async with _denials_async(current_user, "user.update"):
            await asyncio.to_thread(deny_unless, current_user.user_id, "users.write")
            user, stale_reason, payload = await asyncio.to_thread(
                _apply_user_update, session, current_user, user_id, request
            )

        # Sign the user out everywhere so their next session carries the new
        # email or role, not the claims and permissions cached at login.
        if stale_reason:
            try:
                await revoke_all_for_user(user.user_id)
            except Exception as exc:
                logger.error(
                    "%s for %s; session cutoff is stored but the Redis copy "
                    "failed: %s",
                    stale_reason,
                    user.username,
                    exc,
                )
            # After the Redis round-trip. A refresh during that wait must
            # still be older than the cutoff this request commits.
            stamp_session_cutoff(user)

        logger.info(f"User updated by {current_user.username}: {user.username}")
        return payload

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Update user error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update user",
        )


def _delete_user(session: Session, current_user: User, user_id: str) -> User:
    """Sync half of delete_user: the account row stays, so its principal and
    its credentials still resolve; everything that could act as it stops."""
    # Prevent self-deletion
    if user_id == current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account",
        )

    user = _live_user(session, user_id)
    _require_privileges_cover_target(current_user, user, session, action="delete")
    _refuse_lockout(current_user, session, users={user.user_id: (user.role_id, False)})

    actor = current_user.principal_id
    principals.mark_deleted(session, user.principal_id)
    user.is_active = False
    # Row before trail lock, the order every other path takes them in.
    session.flush()
    # In this transaction: the delete does not happen without its entries.
    trail_events.record(
        session, "user.deleted", actor=actor, principal=user.principal_id
    )
    for credential in mcp_credential_service.list_for_user(user_id, session=session):
        if mcp_credential_service.revoke(credential.credential_id, session=session):
            trail_events.record(
                session,
                "credential.revoked",
                actor=actor,
                principal=user.principal_id,
                credential_id=credential.credential_id,
            )
    sign_ins.end_all_for_user(user_id, sign_ins.REVOKED, session=session)
    session.flush()
    return user


@router.delete("/{user_id}")
async def delete_user(
    user_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """
    Delete a user (requires users.delete permission).

    The username and email stay taken: the row is kept so its principal, and
    the credentials it minted, still resolve on old records.

    Args:
        user_id: User ID to delete
        current_user: Current authenticated user
        session: Database session

    Returns:
        Success message
    """
    async with _denials_async(current_user, "user.delete"):
        await asyncio.to_thread(deny_unless, current_user.user_id, "users.delete")

        user = await asyncio.to_thread(_delete_user, session, current_user, user_id)
    try:
        await revoke_all_for_user(user.user_id, session=session)
    except Exception as exc:
        # The account is inactive and its sign-ins ended, so no refresh and no
        # new login succeeds; an access token already out lives until expiry.
        logger.error("User %s deleted but revoke_all_for_user failed: %s", user_id, exc)

    logger.info(f"User deleted by {current_user.username}: {user.username}")
    return {"message": "User deleted successfully"}


@router.get("/roles/list")
def list_roles(
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """
    List all available roles.

    Args:
        current_user: Current authenticated user
        session: Database session

    Returns:
        List of roles
    """
    with _denials(current_user, "role.list"):
        deny_unless(current_user.user_id, "users.read")
    try:
        holders = dict(
            session.query(User.role_id, func.count()).group_by(User.role_id).all()
        )
        roles = session.query(Role).order_by(Role.created_at, Role.role_id).all()
        return {
            "roles": [_role_entry(r, holders.get(r.role_id, 0)) for r in roles],
            "presets": [{"key": key, **preset} for key, preset in PRESETS.items()],
            "permissions": sorted(KNOWN_PERMISSIONS),
            "platform_equivalent": sorted(PLATFORM_EQUIVALENT),
            "nobody_can_manage_roles": _role_managers(session) == 0,
        }

    except Exception as e:
        logger.error(f"List roles error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list roles",
        )


def _role_entry(role: Role, holders: int) -> dict:
    entry = RoleSchema.dump(role)
    if role.role_id in CATALOG["builtins"]:
        # Edited in this install's database: kept, and shown with the diff.
        diff = builtin_diff(role.role_id, role.permissions)
        entry.update(kind="builtin", modified=bool(diff), diff=diff)
    else:
        entry["kind"] = "legacy" if role.role_id in LEGACY_ROLE_IDS else "custom"
    entry["unrecognised"] = sorted(set(role.permissions or {}) - KNOWN_PERMISSIONS)
    entry["holders"] = holders
    return entry


def _require_permission(current_user: User, permission: str, session) -> None:
    deny_unless(current_user.user_id, permission, session)


def _known(permissions: dict) -> dict:
    unknown = sorted(set(permissions) - KNOWN_PERMISSIONS)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown permission: {', '.join(unknown)}",
        )
    return dict(permissions)


def _role_name(session: Session, name: str, role_id: Optional[str] = None) -> str:
    name = name.strip()
    if not name or len(name) > 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A role name is 1 to 100 characters",
        )
    # Unique across built-ins too, case aside.
    query = session.query(Role).filter(func.lower(Role.name) == name.lower())
    if role_id:
        query = query.filter(Role.role_id != role_id)
    if query.first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A role named {name} already exists",
        )
    return name


def _editable_role(session: Session, role_id: str) -> Role:
    role = session.get(Role, role_id)
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Role not found"
        )
    if role.role_id in CATALOG["builtins"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=BUILTIN_ROLE_REFUSED
        )
    return role


def _record_role(
    session: Session, action: str, current_user: User, role: Role, before, after
) -> None:
    trail_events.record(
        session,
        action,
        actor=current_user.principal_id,
        role_id=role.role_id,
        permissions_before=sorted(_granted(before)),
        permissions_after=sorted(_granted(after)),
    )


@router.post("/roles", status_code=status.HTTP_201_CREATED)
def create_role(
    request: CreateRoleRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """Create a custom role, from scratch or as a copy of a preset or role."""
    with _denials(current_user, "role.create"):
        _require_permission(current_user, ROLES_MANAGE, session)
        permissions = request.permissions
        if request.copy_from:
            preset = PRESETS.get(request.copy_from)
            source = session.get(Role, request.copy_from) if preset is None else None
            if preset is None and source is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="No preset or role to copy",
                )
            copied = preset["permissions"] if preset else source.permissions
            if permissions is None:
                # A legacy role's unrecognised keys have no effect; not copied.
                permissions = {
                    k: v for k, v in (copied or {}).items() if k in KNOWN_PERMISSIONS
                }
        permissions = _known(permissions or {})
        name = _role_name(session, request.name)
        _refuse_uncovered(current_user, permissions, session, "Cannot create this role")
        _refuse_machine_widening(current_user, session, None, permissions)
        role = Role(
            role_id=f"role-custom-{uuid.uuid4().hex[:12]}",
            name=name,
            description=request.description.strip(),
            permissions=permissions,
            is_system_role=False,
        )
        session.add(role)
        session.flush()
        _record_role(session, "role.created", current_user, role, {}, permissions)
    logger.info("Role %s created by %s", role.role_id, current_user.username)
    return _role_entry(role, 0)


@router.put("/roles/{role_id}")
def update_role(
    role_id: str,
    request: UpdateRoleRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """Edit a custom role. The editor holds everything it grants, before and after."""
    with _denials(current_user, "role.update"):
        _require_permission(current_user, ROLES_MANAGE, session)
        role = _editable_role(session, role_id)
        before = dict(role.permissions or {})
        after = (
            _known(request.permissions) if request.permissions is not None else before
        )
        involved = {key: True for key in _granted(before) | _granted(after)}
        _refuse_uncovered(current_user, involved, session, "Cannot edit this role")
        _refuse_machine_widening(current_user, session, before, after)
        machine_holds = (
            _not_deleted(session.query(User))
            .filter(User.role_id == role.role_id, Principal.kind != principals.PERSON)
            .first()
        )
        if "audit.read" in _granted(after) - _granted(before) and machine_holds:
            _refuse_audit_read_to_machine(current_user, session)
        _refuse_lockout(current_user, session, roles={role.role_id: after})
        if request.name is not None:
            role.name = _role_name(session, request.name, role.role_id)
        if request.description is not None:
            role.description = request.description.strip()
        role.permissions = after
        session.flush()
        _record_role(session, "role.updated", current_user, role, before, after)
        holders = session.query(User).filter(User.role_id == role.role_id).count()
    logger.info("Role %s updated by %s", role.role_id, current_user.username)
    return _role_entry(role, holders)


@router.delete("/roles/{role_id}")
def delete_role(
    role_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """Delete a custom role nobody holds."""
    with _denials(current_user, "role.delete"):
        _require_permission(current_user, ROLES_MANAGE, session)
        role = _editable_role(session, role_id)
        _refuse_uncovered(
            current_user, role.permissions, session, "Cannot delete this role"
        )
        # Deleted accounts keep their row, and their role with it.
        holders = session.query(User).filter(User.role_id == role.role_id).count()
        if holders:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{holders} account{'s' if holders != 1 else ''} "
                "still hold this role. Move them to another role first.",
            )
        before = dict(role.permissions or {})
        session.delete(role)
        session.flush()
        _record_role(session, "role.deleted", current_user, role, before, {})
    logger.info("Role %s deleted by %s", role_id, current_user.username)
    return {"message": "Role deleted"}
