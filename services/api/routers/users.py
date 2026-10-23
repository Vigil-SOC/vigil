"""
User Management API - Admin endpoints for managing users.

Handles user CRUD operations, role assignment, and user administration.
"""

import asyncio
import logging
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from core.auth import mcp_credential_service, principals, sign_ins, trail_events
from core.auth.auth_service import AuthService
from core.auth.password_validator import PasswordPolicyError, validate_password_strength
from core.auth.session_cutoff import stamp_session_cutoff
from core.auth.token_blacklist import revoke_all_for_user
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import Principal, Role, User
from core.storage.schemas import RoleSchema, UserSchema
from core.storage.unit_of_work import unit_of_work
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


# A service account (Medic's gateway, D2-17) is exempt from lockout, which is
# safe only while it can read no more than a Viewer.
SERVICE_ACCOUNT_ROLE_ID = "role-viewer"


def _is_service_account(user: User) -> bool:
    # Only an explicit True is one; a user object without the field is not.
    try:
        return user.service_account is True
    except AttributeError:
        return False


def _refuse_service_account_role(user: User, role_id: str) -> None:
    if _is_service_account(user) and role_id != SERVICE_ACCOUNT_ROLE_ID:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A service account can only hold the Viewer role",
        )


def _can_assign_role(current_user: User, target_role: Role, session: Session) -> bool:
    """Return True only if current_user holds every permission granted by target_role.

    Prevents a user with users.write from assigning a role that grants
    more privileges than they themselves have. The same comparison covers the
    account being edited: its current role is a privilege the caller must
    already hold before they may change or delete that account.
    """
    current_perms = AuthService.get_user_permissions(current_user.user_id, session)
    for perm, granted in (target_role.permissions or {}).items():
        if granted and not current_perms.get(perm, False):
            return False
    return True


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
        if not AuthService.check_permission(current_user.user_id, "users.read"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Permission denied: users.read required",
            )

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
            "users": UserSchema.dump_many(users),
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
            if not AuthService.check_permission(current_user.user_id, "users.read"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Permission denied: users.read required",
                )

    user = _live_user(session, user_id)
    user_dict = UserSchema.dump(user)

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
        return _create_user(request, current_user, session)


def _create_user(request: CreateUserRequest, current_user: User, session: Session):
    # Check permission
    if not AuthService.check_permission(current_user.user_id, "users.write"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: users.write required",
        )

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

    if not _can_assign_role(current_user, role, session):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot assign a role with more privileges than your own",
        )

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
    if not AuthService.check_permission(current_user.user_id, "users.write"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: users.write required",
        )

    user = _live_user(session, user_id)
    _require_privileges_cover_target(current_user, user, session, action="update")

    # Every check before any write: a disable commits ahead of the rest.
    if request.email is not None:
        if _is_service_account(user):
            # A reachable address is the first step to a password reset.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="A service account's email can't be changed",
            )
        existing = (
            session.query(User)
            .filter(User.email == request.email, User.user_id != user_id)
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already in use",
            )

    if request.role_id is not None:
        role = session.query(Role).filter(Role.role_id == request.role_id).first()
        if not role:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role ID"
            )
        if not _can_assign_role(current_user, role, session):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot assign a role with more privileges than your own",
            )
        _refuse_service_account_role(user, request.role_id)

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

    if request.email is not None:
        if request.email != user.email:
            changed.append("email")
        user.email = request.email
        user.is_verified = False
        stale_reason = "Email changed"

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
                    {"role_id": request.role_id, "previous_role_id": user.role_id},
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
        if not await asyncio.to_thread(
            AuthService.check_permission, current_user.user_id, "users.delete"
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Permission denied: users.delete required",
            )

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
        if not AuthService.check_permission(current_user.user_id, "users.read"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Permission denied: users.read required",
            )
    try:
        roles = session.query(Role).all()
        return {"roles": RoleSchema.dump_many(roles)}

    except Exception as e:
        logger.error(f"List roles error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list roles",
        )
