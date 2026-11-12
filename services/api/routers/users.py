"""
User Management API - Admin endpoints for managing users.

Handles user CRUD operations, role assignment, and user administration.
"""

import asyncio
import logging
import re
import uuid
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from core import teams
from core.audit import retention
from core.auth import current_user as current_user_module
from core.auth import (
    dormancy,
    invites,
    mcp_credential_service,
    mfa_methods,
    principals,
    security_keys,
    session_policy,
    sign_ins,
    sso,
    sso_settings,
    step_up,
    trail_events,
)
from core.auth.auth_cookies import clear_auth_cookies
from core.auth.auth_service import (
    PERSON_ONLY,
    AuthService,
    _account_is_locked,
    clear_lock,
    managers,
)
from core.auth.deactivation import deactivate_account, when_committed
from core.auth.identity import claim_sign_in_names, identifier_in_use
from core.auth.password_reset import reset_link
from core.auth.password_validator import USERS_LINK
from core.auth.permissions import (
    CATALOG,
    KNOWN_PERMISSIONS,
    PLATFORM_EQUIVALENT,
    PRESETS,
    STEP_UP,
    builtin_diff,
    deny_unless,
    missing_permission,
    require_person_permission,
)
from core.auth.service_account import USERNAME_PATTERN as MEDIC_USERNAME_PATTERN
from core.config import get_settings
from core.platform.email_service import mail_is_configured, send_email
from core.platform.notices import notify
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import AuthLink, Case, Principal, Role, Team, User
from core.storage.schemas import RoleSchema, UserSchema
from core.storage.schemas.base import IsoDateTime
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow
from core.upgrade.adopt import LEGACY_ROLE_IDS
from services.api.middleware.auth import get_current_user
from services.api.middleware.client_address import client_host

logger = logging.getLogger(__name__)

router = APIRouter()


def _step_up(permission: str):
    """Ask "Confirm it's you" before a write that holds ``permission``."""
    return step_up.gate(permission, STEP_UP[permission])


ROUTER_META = RouterMeta(
    prefix="/api/users",
    tags=["users"],
    auth=Auth.REQUIRED,
)


# Request/Response Models
SERVICE_ACCOUNT_NAME = r"^[a-z0-9][a-z0-9._-]{2,63}$"


class CreateServiceAccountRequest(BaseModel):
    """Create a token-only account for an integration, with the person answerable for it."""

    name: str = Field(pattern=SERVICE_ACCOUNT_NAME)
    purpose: str = Field(min_length=1, max_length=500)
    role_id: str = "role-viewer"
    # The accountable person's user id (their principal id).
    accountable: str

    @field_validator("purpose")
    @classmethod
    def _purpose(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A purpose is required")
        return value


class ReassignRequest(BaseModel):
    accountable: str


class UpdateUserRequest(BaseModel):
    """Update user request: profile and role. Status changes have their own routes."""

    full_name: Optional[str] = None
    email: Optional[EmailStr] = None
    role_id: Optional[str] = None


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
    """A person's own session. A service account's is not, nor is any API token's;
    DEV_MODE's dev user is."""
    if _is_service_account(user) or current_user_module.is_token_request():
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


def _manager_accounts(
    session: Session,
    roles: Optional[dict[str, Optional[dict]]] = None,
    users: Optional[dict[str, tuple[str, bool]]] = None,
) -> list[User]:
    """Active people who can sign in (a password, or a single sign-on link while
    it is on; only the link while it is required) whose role holds roles.manage,
    with ``roles`` ({id: permissions}) and ``users`` ({id: (role_id, is_active)})
    applied as if saved. Locked ones included."""
    held = {role.role_id: role.permissions for role in session.query(Role)}
    held.update(roles or {})
    people = (
        _not_deleted(session.query(User)).filter(Principal.kind == principals.PERSON)
    ).all()
    current = sso_settings.settings()
    linked = sso.linked_principals(session) if sso.is_on(current) else set()
    required = sso.required(current)
    found = []
    for user in people:
        role_id, active = (users or {}).get(
            user.user_id, (user.role_id, user.is_active)
        )
        if (
            active
            and (user.principal_id in linked or (user.password_hash and not required))
            and (held.get(role_id) or {}).get(ROLES_MANAGE)
        ):
            found.append(user)
    return found


def _role_managers(session: Session, **change) -> int:
    """Role managers who can sign in now: _manager_accounts less the locked."""
    return sum(
        1
        for user in _manager_accounts(session, **change)
        if not _account_is_locked(user)
    )


def _leaves_no_manager(session: Session, **change) -> bool:
    """Whether ``change`` takes roles.manage from someone and leaves nobody who
    can sign in holding it. Counted when all are locked too: removing a locked
    manager still narrows who could be unlocked."""
    before = {user.user_id for user in _manager_accounts(session)}
    after = {user.user_id for user in _manager_accounts(session, **change)}
    return bool(before - after) and not _role_managers(session, **change)


def _refuse_lockout(current_user: User, session: Session, **change) -> None:
    """A person may not leave nobody able to manage roles. A machine's
    narrowing is containment and stands; the roles list then says so."""
    if not _acts_as_person(current_user, session):
        return
    if _leaves_no_manager(session, **change):
        locked = sorted(
            user.username
            for user in _manager_accounts(session, **change)
            if _account_is_locked(user)
        )
        detail = f"This would leave no person holding {ROLES_MANAGE} who can sign in"
        if locked:
            detail += f". Locked, and would count once unlocked: {', '.join(locked)}"
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


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


def _is_erased(user: User) -> bool:
    return retention.is_erased(user.username, user.principal_id)


def _refuse_erased(user: User) -> None:
    if _is_erased(user):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This account was erased; it can't be reactivated or restored",
        )


def _account_status(user: User, deleted: bool) -> str:
    if _is_erased(user):
        return "erased"
    if deleted:
        return "deleted"
    if not user.is_active:
        return "deactivated"
    return "locked" if _account_is_locked(user) else "active"


AccountStatus = Literal[
    "current", "all", "active", "locked", "deactivated", "deleted", "erased"
]


def _filter_status(query, wanted: AccountStatus):
    """``query`` (joined to Principal) narrowed to one status. "current" is
    every account not deleted or erased; "all" is every account."""
    erased = User.username == func.concat(retention.ERASED_PREFIX, User.principal_id)
    deleted = Principal.deleted_at.isnot(None)
    if wanted == "all":
        return query
    if wanted == "erased":
        return query.filter(erased)
    if wanted == "deleted":
        return query.filter(deleted, ~erased)
    query = query.filter(~deleted, ~erased)
    # As _account_is_locked: a timed lock until it ends, else until cleared.
    now = utcnow()
    locked = and_(
        User.service_account.isnot(True),
        or_(
            User.locked_until > now,
            and_(User.locked_until.is_(None), User.locked_at.isnot(None)),
        ),
    )
    if wanted == "deactivated":
        return query.filter(User.is_active.is_(False))
    if wanted == "locked":
        return query.filter(User.is_active.is_(True), locked)
    if wanted == "active":
        return query.filter(User.is_active.is_(True), ~locked)
    return query


def _dump_accounts(session: Session, users: list[User]) -> list[dict]:
    """Each account, with its status, what kind of principal it is and which
    of its role's permissions have no effect for it."""
    held = {role.role_id: role.permissions for role in session.query(Role)}
    days = session_policy.policy().dormancy_days
    last = dormancy.activity(session, [u.user_id for u in users]) if days else {}
    ids = [u.principal_id for u in users]
    deleted = {
        pid
        for (pid,) in session.query(Principal.principal_id).filter(
            Principal.principal_id.in_(ids), Principal.deleted_at.isnot(None)
        )
    }
    # Shown before a deactivate or delete, as deactivation tells managers.
    open_cases = dict(
        session.query(Case.assignee, func.count())
        .filter(Case.assignee.in_([u.username for u in users]), Case.status != "closed")
        .group_by(Case.assignee)
        .all()
    )
    linked = sso.linked_principals(session)
    # Who each service account answers to: worked out here, never stored.
    answerable = {
        user.principal_id: (user, gone is not None)
        for user, gone in session.query(User, Principal.deleted_at)
        .join(Principal, Principal.principal_id == User.principal_id)
        .filter(
            User.principal_id.in_(
                [
                    u.accountable_principal_id
                    for u in users
                    if u.accountable_principal_id
                ]
            )
        )
    }
    now = utcnow()
    dumped = []
    for user in users:
        entry = UserSchema.dump(user)
        # How the account signs in (#4285); never the hash or the subject.
        entry["has_password"] = bool(user.password_hash)
        entry["has_sso"] = user.principal_id in linked
        entry["status"] = _account_status(user, user.principal_id in deleted)
        entry["open_cases"] = open_cases.get(user.username, 0)
        machine = _is_service_account(user)
        entry["principal_kind"] = (
            principals.SERVICE_ACCOUNT if machine else principals.PERSON
        )
        entry["managed_by_medic"] = _is_medic(user)
        entry["purpose"] = user.purpose
        entry["accountable"] = None
        entry["accountable_gone"] = False
        if machine and not _is_medic(user):
            person, deleted_person = answerable.get(
                user.accountable_principal_id, (None, True)
            )
            entry["accountable_gone"] = (
                deleted_person or not person.is_active or _is_erased(person)
            )
            if person is not None and not _is_erased(person):
                entry["accountable"] = {
                    "user_id": person.user_id,
                    "username": person.username,
                    "full_name": person.full_name,
                }
        # Stored on the role. AuthService.check_permission refuses them for a
        # non-person, so for this account they do nothing.
        entry["no_effect"] = (
            sorted(PERSON_ONLY & _granted(held.get(user.role_id))) if machine else []
        )
        # Whether sign-in is refused now, and when and from where the lock was set.
        # locked_until is empty for a lock that lasts until an Unlock or a reset.
        entry["locked"] = _account_is_locked(user)
        entry["locked_until"] = (
            user.locked_until and user.locked_until.isoformat() + "Z"
        )
        entry["locked_at"] = user.locked_at and user.locked_at.isoformat() + "Z"
        entry["locked_from"] = user.locked_from
        # When an idle account will be deactivated, once it is inside the warning window.
        entry["deactivating_at"] = None
        if days and user.is_active and not machine and user.user_id in last:
            if now >= dormancy.warning_start(last[user.user_id], days):
                entry["deactivating_at"] = (
                    dormancy.deadline(last[user.user_id], days).isoformat() + "Z"
                )
        dumped.append(entry)
    return dumped


@router.get("/")
def list_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    role_id: Optional[str] = None,
    is_active: Optional[bool] = None,
    service_account: Optional[bool] = None,
    search: Optional[str] = None,
    account_status: AccountStatus = Query("current", alias="status"),
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
        service_account: Only service accounts (true) or only people (false)
        search: Search in username, email, or full name
        account_status: "current" (not deleted or erased), "all", or one status
        current_user: Current authenticated user
        session: Database session

    Returns:
        List of users
    """
    # Check permission
    with _denials(current_user, "user.list"):
        deny_unless(current_user.user_id, "users.read")

    try:
        query = _filter_status(
            session.query(User).join(
                Principal, Principal.principal_id == User.principal_id
            ),
            account_status,
        )

        # Apply filters
        if role_id:
            query = query.filter(User.role_id == role_id)

        if is_active is not None:
            query = query.filter(User.is_active == is_active)

        if service_account is not None:
            query = query.filter(
                User.service_account.is_(True)
                if service_account
                else User.service_account.isnot(True)
            )

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
        users = query.order_by(User.username).offset(skip).limit(limit).all()

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


class PersonTeam(BaseModel):
    """One team a person is on. Archived teams are left out: they accept no one."""

    id: str
    name: str
    is_default: bool


class Person(BaseModel):
    """One current person, as a picker shows them."""

    username: str
    display_name: Optional[str] = None
    teams: list[PersonTeam] = []


@router.get("/people", response_model=list[Person])
def list_people(
    session: UnitOfWorkSession,
    current_user: Annotated[
        User,
        Depends(
            require_person_permission(
                "cases.read", "cases.assign", "cases.write", refuse_machine=True
            )
        ),
    ],
):
    """Current people, for the owner picker and for My teams.

    Current is ``owner_states``'s test: active, not a service account, not
    deleted. cases.assign or cases.write sees everyone. cases.read sees only
    the signed-in person, which is enough to send that person's team ids.
    """
    where = [
        User.is_active.is_(True),
        User.service_account.is_not(True),
        Principal.deleted_at.is_(None),
    ]
    # The full directory is the picker. A reader who cannot assign still needs
    # their own teams for the queue's My teams filter.
    if missing_permission(
        current_user.user_id, "cases.assign", session
    ) and missing_permission(current_user.user_id, "cases.write", session):
        where.append(User.user_id == current_user.user_id)
    rows = session.execute(
        select(User.username, User.full_name)
        .join(Principal, Principal.principal_id == User.principal_id)
        .where(*where)
        .order_by(
            func.lower(func.coalesce(func.nullif(User.full_name, ""), User.username))
        )
    ).all()
    held = teams.teams_for(session, {username for username, _name in rows})
    return [
        Person(
            username=username,
            display_name=(name or "").strip() or None,
            teams=[
                PersonTeam(id=team.team_id, name=team.name, is_default=team.is_default)
                for team in held.get(username, [])
            ],
        )
        for username, name in rows
    ]


class InviteRequest(BaseModel):
    """Invite an address to set up their own account."""

    email: EmailStr = Field(max_length=200)
    role_id: str
    channel: Literal["email", "copy"]
    # Named instead of the default team. Needs teams.manage; absent joins the default.
    team_ids: Optional[list[str]] = None


class ResendRequest(BaseModel):
    channel: Literal["email", "copy"]


class InviteEntry(BaseModel):
    link_id: str
    email: str
    role_id: str
    invited_by: Optional[str] = None
    invited_by_name: Optional[str] = None
    channel: Literal["email", "copy"]
    issued_at: IsoDateTime
    expires_at: IsoDateTime
    expired: bool
    team_ids: list[str] = []


class InviteList(BaseModel):
    """The pending invites, and what Email needs before it can be offered."""

    invites: list[InviteEntry]
    email_missing: list[str]


class InviteIssued(BaseModel):
    """A new link. ``token`` and ``link`` come back once, for a copied link only;
    ``link`` is empty when VIGIL_FRONTEND_URL isn't set and the console builds
    it from its own address."""

    invite: InviteEntry
    token: Optional[str] = None
    link: Optional[str] = None


def _invite_entry(session: Session, row: AuthLink) -> InviteEntry:
    who = (
        session.query(User.full_name, User.username)
        .filter(User.principal_id == row.invited_by)
        .first()
    )
    return InviteEntry(
        link_id=row.link_id,
        email=row.email or "",
        role_id=row.role_id or "",
        invited_by=row.invited_by,
        invited_by_name=(who and (who.full_name or who.username)) or None,
        channel=row.channel or "copy",
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        expired=invites.is_expired(row),
        team_ids=list(row.team_ids or []),
    )


def _email_missing() -> list[str]:
    """What has to be set before an invite can be emailed; empty when it can."""
    missing = []
    if not mail_is_configured():
        missing.append("VIGIL_EMAIL_BACKEND=smtp")
    if not get_settings().vigil_frontend_url.strip():
        missing.append("VIGIL_FRONTEND_URL")
    return missing


def _refuse_invite_actor(current_user: User, session: Session) -> None:
    """Inviting is a person's own act, and the invite is theirs to answer for."""
    if not _acts_as_person(current_user, session) or not _principal_kind(
        current_user, session
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inviting needs a person's own sign-in",
        )


def _refuse_held_address(session: Session, email: str) -> None:
    """An address an account already holds is not invited: bring that account back."""
    held = (
        session.query(User, Principal.deleted_at)
        .join(Principal, Principal.principal_id == User.principal_id)
        .filter(func.lower(User.email) == email.casefold())
        .first()
    )
    if held:
        user, deleted_at = held
        if deleted_at is not None:
            detail = (
                f"{email} belongs to a deleted account. Restore it instead of inviting."
            )
        elif not user.is_active:
            detail = f"{email} belongs to a deactivated account. Reactivate it instead of inviting."
        else:
            detail = f"{email} already has an account."
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
    # Its own pending invite is the one being replaced.
    mine = invites.open_for(session, email)
    if identifier_in_use(session, email, except_link_id=mine and mine.link_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{email} is already used as a sign-in name.",
        )


def _issue_invite(
    session: Session,
    current_user: User,
    email: str,
    role_id: str,
    channel: str,
    team_ids: Optional[list[str]] = None,
) -> InviteIssued:
    settings = get_settings()
    base = settings.vigil_frontend_url.strip()
    if channel == invites.EMAIL and (missing := _email_missing()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Email isn't available: set {' and '.join(missing)}, "
            "or copy the link instead.",
        )
    role = session.get(Role, role_id)
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role ID"
        )
    _refuse_uncovered(
        current_user, role.permissions, session, "Cannot invite to this role"
    )
    # An address is a sign-in name: hold it so two invites or sign-ups can't race.
    claim_sign_in_names(session, email)
    _refuse_held_address(session, email)
    chosen = teams.check_invite_teams(session, team_ids)
    row, token = invites.issue(
        session, email, role_id, current_user.principal_id, channel, chosen
    )
    entry = _invite_entry(session, row)
    if channel == invites.EMAIL:
        link = invites.link_url(base, token)
        who = current_user.full_name or current_user.username
        when_committed(
            session,
            lambda: send_email(
                to=email,
                subject="You're invited to Vigil SOC",
                body=(
                    f"{who} invited you to Vigil SOC as {role.name}.\n\n"
                    "Open the link below to choose your username and password. "
                    "It is valid for 24 hours and can only be used once.\n\n"
                    f"{link}\n\n"
                    "If you weren't expecting this, you can ignore this email."
                ),
            ),
        )
        return InviteIssued(invite=entry)
    return InviteIssued(
        invite=entry, token=token, link=invites.link_url(base, token) if base else None
    )


@router.get("/invites", response_model=InviteList)
def list_invites(
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """Invites not yet accepted or revoked, expired ones too (users.write)."""
    with _denials(current_user, "invite.list"):
        deny_unless(current_user.user_id, "users.write")
    rows = invites.pending(session)
    for row in rows:
        invites.note_expired(session, row)
    return InviteList(
        invites=[_invite_entry(session, row) for row in rows],
        email_missing=_email_missing(),
    )


@router.post(
    "/invites",
    response_model=InviteIssued,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_step_up("users.write")],
)
def create_invite(
    request: InviteRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """Invite an address, replacing its pending invite if it has one."""
    with _denials(current_user, "invite.create"):
        deny_unless(current_user.user_id, "users.write")
        _refuse_invite_actor(current_user, session)
        if any((team_id or "").strip() for team_id in (request.team_ids or [])):
            deny_unless(current_user.user_id, "teams.manage")
        return _issue_invite(
            session,
            current_user,
            str(request.email),
            request.role_id,
            request.channel,
            request.team_ids,
        )


@router.post(
    "/invites/{link_id}/resend",
    response_model=InviteIssued,
    dependencies=[_step_up("users.write")],
)
def resend_invite(
    link_id: str,
    request: ResendRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """A new link for a pending invite; the earlier one stops working."""
    with _denials(current_user, "invite.resend"):
        deny_unless(current_user.user_id, "users.write")
        _refuse_invite_actor(current_user, session)
        row = invites.get(session, link_id)
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found"
            )
        return _issue_invite(
            session,
            current_user,
            row.email,
            row.role_id,
            request.channel,
            list(row.team_ids or []),
        )


@router.delete("/invites/{link_id}", dependencies=[_step_up("users.write")])
def revoke_invite(
    link_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """Cancel a pending invite; its link stops working."""
    with _denials(current_user, "invite.revoke"):
        deny_unless(current_user.user_id, "users.write")
        _refuse_invite_actor(current_user, session)
    row = invites.get(session, link_id, lock=True)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found"
        )
    invites.revoke(session, row, current_user.principal_id)
    return {"message": "Invite revoked"}


class TeamOut(BaseModel):
    id: str
    name: str
    is_default: bool
    archived: bool


class TeamMemberOut(BaseModel):
    user_id: str
    username: str
    display_name: Optional[str] = None


class TeamDetail(TeamOut):
    members: list[TeamMemberOut]


class TeamName(BaseModel):
    name: str = Field(min_length=1, max_length=teams.NAME_MAX)


class TeamMemberAdd(BaseModel):
    user_id: str


class TeamMemberRemoved(BaseModel):
    user_id: str
    warning: Optional[str] = None


def _team_out(team) -> TeamOut:
    return TeamOut(
        id=team.team_id,
        name=team.name,
        is_default=team.is_default,
        archived=team.archived_at is not None,
    )


def _team_detail(session: Session, team) -> TeamDetail:
    return TeamDetail(
        **_team_out(team).model_dump(),
        members=[
            TeamMemberOut(
                user_id=user.user_id,
                username=user.username,
                display_name=(user.full_name or "").strip() or None,
            )
            for user in teams.members_of(session, team.team_id)
        ],
    )


@router.get("/teams", response_model=list[TeamOut])
def list_teams(
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """Every team, archived included (users.read)."""
    with _denials(current_user, "team.list"):
        deny_unless(current_user.user_id, "users.read")
    return [_team_out(team) for team in teams.list_teams(session)]


@router.post("/teams", response_model=TeamOut, status_code=status.HTTP_201_CREATED)
def create_team(
    body: TeamName,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """Create a team (teams.manage)."""
    with _denials(current_user, "team.create"):
        deny_unless(current_user.user_id, "teams.manage")
    return _team_out(
        teams.create_team(session, body.name, actor=current_user.principal_id)
    )


@router.get("/teams/{team_id}", response_model=TeamDetail)
def get_team(
    team_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """One team and its members (users.read)."""
    with _denials(current_user, "team.read"):
        deny_unless(current_user.user_id, "users.read")
    team = session.get(Team, team_id)
    if team is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Team not found"
        )
    return _team_detail(session, team)


@router.post("/teams/{team_id}/rename", response_model=TeamOut)
def rename_team(
    team_id: str,
    body: TeamName,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """Rename a team, archived names included in the uniqueness check (teams.manage)."""
    with _denials(current_user, "team.rename"):
        deny_unless(current_user.user_id, "teams.manage")
    return _team_out(
        teams.rename_team(session, team_id, body.name, actor=current_user.principal_id)
    )


@router.post("/teams/{team_id}/archive", response_model=TeamOut)
def archive_team(
    team_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """Archive a team once every case on it is closed. The default team cannot be."""
    with _denials(current_user, "team.archive"):
        deny_unless(current_user.user_id, "teams.manage")
    return _team_out(
        teams.archive_team(session, team_id, actor=current_user.principal_id)
    )


@router.post("/teams/{team_id}/unarchive", response_model=TeamOut)
def unarchive_team(
    team_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """Bring an archived team back (teams.manage)."""
    with _denials(current_user, "team.unarchive"):
        deny_unless(current_user.user_id, "teams.manage")
    return _team_out(
        teams.unarchive_team(session, team_id, actor=current_user.principal_id)
    )


@router.post(
    "/teams/{team_id}/members",
    response_model=TeamDetail,
    status_code=status.HTTP_201_CREATED,
)
def add_team_member(
    team_id: str,
    body: TeamMemberAdd,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """Add a person to a team. An archived team accepts no one (teams.manage)."""
    with _denials(current_user, "team.member_add"):
        deny_unless(current_user.user_id, "teams.manage")
    teams.add_member(session, team_id, body.user_id, actor=current_user.principal_id)
    return _team_detail(session, session.get(Team, team_id))


@router.delete("/teams/{team_id}/members/{user_id}", response_model=TeamMemberRemoved)
def remove_team_member(
    team_id: str,
    user_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """Remove a person from a team (teams.manage)."""
    with _denials(current_user, "team.member_remove"):
        deny_unless(current_user.user_id, "teams.manage")
    warning = teams.remove_member(
        session, team_id, user_id, actor=current_user.principal_id
    )
    return TeamMemberRemoved(user_id=user_id, warning=warning)


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


class EndSignInsRequest(BaseModel):
    """Why a manager, service account or playbook ends sign-ins. Kept in the trail."""

    reason: str = Field(max_length=500)

    @field_validator("reason")
    @classmethod
    def _typed(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A reason is required")
        return value


def _managed_sign_ins(
    current_user: User, session: Session, user_id: str, action: str
) -> User:
    """The person, for a holder of users.write who covers their role. Anyone
    else gets a 403, so the screen shows them nothing."""
    with _denials(current_user, f"user.sign_ins.{action}"):
        deny_unless(current_user.user_id, "users.write")
        user = _live_user(session, user_id)
        if action == "end" and user.user_id == current_user.user_id:
            # Your own need the fresh password or code check, on /api/auth/sign-ins.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="End your own sign-ins from your account menu",
            )
        _require_privileges_cover_target(
            current_user,
            user,
            session,
            action="see the sign-ins of" if action == "read" else "end the sign-ins of",
        )
    return user


def _end_as_admin(
    request: Request,
    session: Session,
    current_user: User,
    ids: list[tuple[str, str]],
    note: str,
    reason: str = sign_ins.ENDED_BY_ADMIN,
) -> list[tuple[str, str]]:
    """End these (sign-in id, principal id) pairs through ``sign_ins.end``, then
    write an entry for each with the actor and the typed reason. Rows are locked
    before the trail's lock, the order every path takes."""
    ended = [(sid, pid) for sid, pid in ids if sign_ins.end(sid, reason, session)]
    for sid, pid in ended:
        trail_events.record(
            session,
            "auth.sign_in_ended",
            {"source_ip": client_host(request)},
            actor=current_user.principal_id,
            principal=pid,
            sign_in=sid,
            end_reason=reason,
            note=note,
        )
    return ended


def _tell(current_user: User, principal_id: str, count: int, everyone=False) -> None:
    """The person is told. No typed reason and nothing secret in the message:
    notices are emailed."""
    who = current_user.full_name or current_user.username
    notify(
        principal_id,
        "sign_in_ended",
        "Your sign-ins were ended" if count != 1 else "Your sign-in was ended",
        f"{who} ended {'all of ' if everyone else ''}your open sign-in"
        f"{'s' if count != 1 else ''}"
        f"{' (everyone was signed out)' if everyone else ''}. "
        "Sign in again to carry on, and ask them if you were not expecting this.",
        priority="high",
    )


@router.get("/{user_id}/sign-ins", response_model=list[sign_ins.SignInEntry])
def list_user_sign_ins(
    user_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """A person's sign-ins, read-only. users.write over the account, and a role
    the caller covers."""
    user = _managed_sign_ins(current_user, session, user_id, "read")
    return sign_ins.listing(session, user.principal_id)


@router.get("/{user_id}/mfa-methods", response_model=list[mfa_methods.MfaMethod])
def list_user_mfa_methods(
    user_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """A person's sign-in methods, read-only: type, name, added, last used and the
    recovery codes left, never a secret or a code. users.write over the account,
    and a role the caller covers."""
    with _denials(current_user, "user.mfa_methods.read"):
        deny_unless(current_user.user_id, "users.write")
        user = _live_user(session, user_id)
        _require_privileges_cover_target(
            current_user, user, session, action="see the sign-in methods of"
        )
    return mfa_methods.methods(session, user)


@router.post("/sign-ins/end-all", response_model=sign_ins.EndedSignIns)
def end_everyones_sign_ins(
    request: Request,
    response: Response,
    body: EndSignInsRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """Sign the whole install out, the caller too. A person's own act: users.write,
    and every role an active account holds covered, with a typed reason."""
    with _denials(current_user, "user.sign_ins.end_all"):
        deny_unless(current_user.user_id, "users.write")
        if not _acts_as_person(current_user, session):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Signing everyone out needs a person's own sign-in",
            )
        role_ids = {
            role_id
            for (role_id,) in _not_deleted(session.query(User.role_id)).filter(
                User.is_active.is_(True)
            )
        }
        for role in session.query(Role).filter(Role.role_id.in_(role_ids)):
            if not _can_assign_role(current_user, role, session):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cannot sign everyone out: an active account's role "
                    "grants privileges you do not have",
                )
    ended = _end_as_admin(
        request,
        session,
        current_user,
        sign_ins.open_ids(session),
        body.reason,
        sign_ins.EVERYONE,
    )
    trail_events.record(
        session,
        "auth.everyone_signed_out",
        {"source_ip": client_host(request)},
        actor=current_user.principal_id,
        count=len(ended),
        note=body.reason,
    )
    here = False
    for principal_id in {pid for _, pid in ended} - {current_user.principal_id}:
        # People only: a service account or agent has nobody to tell.
        user = session.query(User).filter(User.principal_id == principal_id).first()
        if user is not None and not _is_service_account(user):
            _tell(
                current_user,
                principal_id,
                sum(1 for _, p in ended if p == principal_id),
                True,
            )
    if any(pid == current_user.principal_id for _, pid in ended):
        clear_auth_cookies(response)
        here = True
    return sign_ins.EndedSignIns(ended=len(ended), signed_out=here)


@router.post("/{user_id}/sign-ins/end", response_model=sign_ins.EndedSignIns)
def end_user_sign_ins(
    user_id: str,
    request: Request,
    body: EndSignInsRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """End all of a person's open sign-ins, with a typed reason. A service account
    or playbook holding users.write may call this as containment."""
    user = _managed_sign_ins(current_user, session, user_id, "end")
    ended = _end_as_admin(
        request,
        session,
        current_user,
        sign_ins.open_ids(session, user.principal_id),
        body.reason,
    )
    if ended:
        _tell(current_user, user.principal_id, len(ended))
    return sign_ins.EndedSignIns(ended=len(ended))


@router.post(
    "/{user_id}/sign-ins/{sign_in_id}/end", response_model=sign_ins.EndedSignIns
)
def end_user_sign_in(
    user_id: str,
    sign_in_id: str,
    request: Request,
    body: EndSignInsRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """End one of a person's open sign-ins, with a typed reason."""
    user = _managed_sign_ins(current_user, session, user_id, "end")
    open_ones = sign_ins.open_ids(session, user.principal_id)
    if sign_in_id not in {sid for sid, _ in open_ones}:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Sign-in not found"
        )
    ended = _end_as_admin(
        request, session, current_user, [(sign_in_id, user.principal_id)], body.reason
    )
    if ended:
        _tell(current_user, user.principal_id, 1)
    return sign_ins.EndedSignIns(ended=len(ended))


IdentityConfirmedBy = Literal[
    "in_person", "video_call", "phone_call_back", "other_verified_channel"
]


class UnlockRequest(BaseModel):
    """How the manager confirmed the person is who they say. Kept in the trail.
    ``suspected_compromise`` also revokes the person's API tokens."""

    identity_confirmed_by: IdentityConfirmedBy
    suspected_compromise: bool = False


class ResetLinkRequest(BaseModel):
    """Send the person a reset link, or copy one to hand over. A copied link
    needs how their identity was confirmed."""

    channel: Literal["email", "copy"]
    identity_confirmed_by: Optional[IdentityConfirmedBy] = None
    suspected_compromise: bool = False


class ResetLinkEntry(BaseModel):
    link_id: str
    channel: str
    issued_by_name: Optional[str] = None
    issued_at: IsoDateTime
    expires_at: IsoDateTime


class ResetLinkState(BaseModel):
    """The person's open reset link, if any, and what Email needs first."""

    reset_link: Optional[ResetLinkEntry] = None
    email_missing: list[str]


class ResetLinkIssued(BaseModel):
    """``token`` and ``link`` come back once, for a copied link only; ``link`` is
    empty when VIGIL_FRONTEND_URL isn't set and the console builds it."""

    reset_link: ResetLinkEntry
    token: Optional[str] = None
    link: Optional[str] = None
    tokens_revoked: int = 0


class MfaResetRequest(EndSignInsRequest):
    """A typed reason and how the person's identity was confirmed, both kept."""

    identity_confirmed_by: IdentityConfirmedBy
    suspected_compromise: bool = False


class MfaResetResult(BaseModel):
    ended: int
    tokens_revoked: int = 0
    # Set when the person is the last who can manage roles besides the caller.
    warning: Optional[str] = None


def _recovery_target(
    current_user: User, session: Session, user_id: str, action: str
) -> User:
    """The account a recovery act is for: users.write over it, a role the caller
    covers, a person's own act and never on themselves or a service account."""
    deny_unless(current_user.user_id, "users.write")
    user = _live_user(session, user_id)
    if user.user_id == current_user.user_id or _account_is_locked(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Ask another administrator to {action} this account",
        )
    if not _acts_as_person(current_user, session):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Recovering an account needs a person's own sign-in",
        )
    _require_privileges_cover_target(current_user, user, session, action=action)
    if _is_service_account(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A service account has no password or two-step sign-in to recover",
        )
    return user


def _tell_of_recovery(
    session: Session,
    current_user: User,
    user: User,
    title: str,
    to_person: str,
    to_managers: str,
) -> None:
    """Once committed: the person, and every other holder of users.write but the
    caller. Never the link: notices are emailed."""
    # Read now: the rows are expired once the commit has happened.
    about, actor = user.principal_id, current_user.principal_id
    theirs = title.replace("Your", f"{user.username}'s")

    def send() -> None:
        notify(about, "account_recovery", title, to_person, priority="high")
        for to in managers(about):
            if to == actor:
                continue
            try:
                notify(
                    to,
                    "account_recovery",
                    theirs,
                    to_managers,
                    USERS_LINK,
                    priority="high",
                )
            except Exception:  # noqa: BLE001 - the others are still told
                logger.exception("could not send a recovery notice to %s", to)

    when_committed(session, send)


def _revoke_tokens(session: Session, current_user: User, user: User) -> int:
    """Suspected compromise: every API token the person holds is revoked."""
    revoked = 0
    for credential in mcp_credential_service.list_for_user(
        user.user_id, session=session
    ):
        if mcp_credential_service.revoke(credential.credential_id, session=session):
            trail_events.record(
                session,
                "credential.revoked",
                actor=current_user.principal_id,
                principal=user.principal_id,
                credential_id=credential.credential_id,
                compromised=True,
            )
            revoked += 1
    return revoked


def _reset_entry(session: Session, row: AuthLink) -> ResetLinkEntry:
    who = row.invited_by and (
        session.query(User.full_name, User.username)
        .filter(User.principal_id == row.invited_by)
        .first()
    )
    return ResetLinkEntry(
        link_id=row.link_id,
        channel=row.channel or "",
        issued_by_name=(who and (who.full_name or who.username)) or None,
        issued_at=row.issued_at,
        expires_at=row.expires_at,
    )


@router.post("/{user_id}/unlock")
def unlock_user(
    user_id: str,
    request: Request,
    body: UnlockRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """End a lock and the failure count. users.write over the account, a role the
    caller covers, and a person's own act, not a playbook's. A lock ends no
    sign-in, so a locked caller's open session may not unlock anyone, itself included.
    """
    with _denials(current_user, "user.unlock"):
        deny_unless(current_user.user_id, "users.write")
        user = _live_user(session, user_id)
        if user.user_id == current_user.user_id or _account_is_locked(current_user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Ask another administrator to unlock this account",
            )
        if not _acts_as_person(current_user, session):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Unlocking needs a person's own sign-in",
            )
        _require_privileges_cover_target(current_user, user, session, action="unlock")
    # An expired timed lock leaves its count: Unlock clears that too.
    if not _account_is_locked(user) and not user.failed_login_count:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This account is not locked"
        )
    clear_lock(user)
    trail_events.record(
        session,
        "user.unlocked",
        {"source_ip": client_host(request)},
        actor=current_user.principal_id,
        principal=user.principal_id,
        identity_confirmed_by=body.identity_confirmed_by,
        suspected_compromise=body.suspected_compromise or None,
    )
    if body.suspected_compromise:
        _revoke_tokens(session, current_user, user)
    who = current_user.full_name or current_user.username
    _tell_of_recovery(
        session,
        current_user,
        user,
        "Your account was unlocked",
        f"{who} unlocked your account. If you didn't ask for this, tell an admin now.",
        f"{who} unlocked {user.username}'s account.",
    )
    return _dump_accounts(session, [user])[0]


@router.get("/{user_id}/reset-link", response_model=ResetLinkState)
def get_reset_link(
    user_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """The person's open reset link, never its value. users.write over the account."""
    with _denials(current_user, "user.reset_link.read"):
        deny_unless(current_user.user_id, "users.write")
        user = _live_user(session, user_id)
        _require_privileges_cover_target(
            current_user, user, session, action="see the reset link of"
        )
    rows = invites.open_resets(session, user.principal_id)
    for row in rows:
        invites.note_expired(session, row)
    live = next((r for r in rows if not invites.is_expired(r)), None)
    return ResetLinkState(
        reset_link=live and _reset_entry(session, live), email_missing=_email_missing()
    )


@router.post(
    "/{user_id}/reset-link",
    response_model=ResetLinkIssued,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_step_up("users.write")],
)
def issue_reset_link(
    user_id: str,
    request: Request,
    body: ResetLinkRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """A one-hour, single-use reset link for the person, replacing any open one:
    emailed to their address, or returned once to hand over. It never touches
    two-step sign-in. They and the other holders of users.write are told."""
    with _denials(current_user, "user.reset_link"):
        user = _recovery_target(current_user, session, user_id, "reset the password of")
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This account is deactivated. Reactivate it first.",
            )
        if sso.required():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Single sign-on is required, so no reset links are issued",
            )
        if body.channel == invites.EMAIL and (missing := _email_missing()):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Email isn't available: set {' and '.join(missing)}, "
                "or copy the link instead.",
            )
        if body.channel == invites.COPY and not body.identity_confirmed_by:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Say how you confirmed it is them before copying a link",
            )
    row, token = invites.issue_reset(
        session,
        user,
        body.channel,
        current_user.principal_id,
        identity_confirmed_by=body.identity_confirmed_by,
        suspected_compromise=body.suspected_compromise or None,
    )
    revoked = (
        _revoke_tokens(session, current_user, user) if body.suspected_compromise else 0
    )
    base = get_settings().vigil_frontend_url.strip()
    who = current_user.full_name or current_user.username
    if body.channel == invites.EMAIL:
        link, to = reset_link(base, token), user.email
        when_committed(
            session,
            lambda: send_email(
                to=to,
                subject="Vigil SOC — password reset",
                body=(
                    f"{who} sent you a link to choose a new password.\n\n"
                    "It is valid for one hour and can only be used once.\n\n"
                    f"{link}\n\n"
                    "If you weren't expecting this, tell an administrator."
                ),
            ),
        )
    how = "emailed you" if body.channel == invites.EMAIL else "made"
    _tell_of_recovery(
        session,
        current_user,
        user,
        "Your password reset link",
        f"{who} {how} a link to reset your password. It works once, for one hour. "
        "If you didn't ask for this, tell an admin now.",
        f"{who} issued a password reset link for {user.username}"
        f"{' and revoked their API tokens' if revoked else ''}.",
    )
    entry = _reset_entry(session, row)
    if body.channel == invites.EMAIL:
        return ResetLinkIssued(reset_link=entry, tokens_revoked=revoked)
    return ResetLinkIssued(
        reset_link=entry,
        token=token,
        link=reset_link(base, token) if base else None,
        tokens_revoked=revoked,
    )


@router.delete("/{user_id}/reset-link", dependencies=[_step_up("users.write")])
def revoke_reset_link(
    user_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """Cancel the person's open reset link; it stops working."""
    with _denials(current_user, "user.reset_link.revoke"):
        user = _recovery_target(
            current_user, session, user_id, "revoke the reset link of"
        )
    if not invites.revoke_resets(
        session, user.principal_id, current_user.principal_id, "revoked"
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No open reset link"
        )
    return {"message": "Reset link revoked"}


@router.post(
    "/{user_id}/mfa/reset",
    response_model=MfaResetResult,
    dependencies=[_step_up("users.write")],
)
def reset_user_mfa(
    user_id: str,
    request: Request,
    body: MfaResetRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """Remove every two-step method the person has (the authenticator app,
    security keys, recovery codes) and end their sign-ins, with a typed reason.
    Exempt from the last-manager guard: it warns instead. The MFA requirement
    asks them to set it up again at their next sign-in."""
    with _denials(current_user, "user.mfa_reset"):
        user = _recovery_target(
            current_user, session, user_id, "reset two-step sign-in for"
        )
    user = AuthService._lock_user(session, user.user_id)
    if not (
        user.mfa_enabled
        or user.mfa_secret
        or user.mfa_recovery_codes
        or security_keys.has_keys(session, user.principal_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="They have no two-step sign-in to reset",
        )
    AuthService.clear_mfa(user)
    keys = security_keys.remove_all(session, user.principal_id)
    trail_events.record(
        session,
        "auth.mfa_reset",
        {"source_ip": client_host(request)},
        actor=current_user.principal_id,
        principal=user.principal_id,
        identity_confirmed_by=body.identity_confirmed_by,
        security_keys=keys,
        suspected_compromise=body.suspected_compromise or None,
        note=body.reason,
    )
    ended = _end_as_admin(
        request,
        session,
        current_user,
        sign_ins.open_ids(session, user.principal_id),
        body.reason,
    )
    revoked = (
        _revoke_tokens(session, current_user, user) if body.suspected_compromise else 0
    )
    warning = None
    held = {u.user_id for u in _manager_accounts(session)}
    if user.user_id in held and not held - {user.user_id, current_user.user_id}:
        warning = (
            f"{user.username} is the only other person who can manage roles. "
            "They must set up two-step sign-in again at their next sign-in."
        )
    who = current_user.full_name or current_user.username
    _tell_of_recovery(
        session,
        current_user,
        user,
        "Your two-step sign-in was reset",
        f"{who} removed your two-step sign-in methods and recovery codes, and "
        "ended your sign-ins. Set two-step sign-in up again when you next sign "
        "in. If you didn't ask for this, tell an admin now.",
        f"{who} reset {user.username}'s two-step sign-in and ended their sign-ins"
        f"{' and revoked their API tokens' if revoked else ''}.",
    )
    return MfaResetResult(ended=len(ended), tokens_revoked=revoked, warning=warning)


@router.post(
    "/service-accounts",
    status_code=status.HTTP_201_CREATED,
    dependencies=[_step_up("users.write")],
)
def create_service_account(
    request: CreateServiceAccountRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """Create a service account: no password, no MFA, signed in by token only.
    users.write, a person's own act, and a role the caller covers; a role holding
    audit.read needs audit.forward too. Tokens are minted on /api/tokens."""
    with _denials(current_user, "user.create_service_account"):
        deny_unless(current_user.user_id, "users.write")
        if not _acts_as_person(current_user, session):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Creating a service account needs a person's own sign-in",
            )
        role = session.get(Role, request.role_id)
        if role is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role ID"
            )
        _refuse_uncovered(
            current_user, role.permissions, session, "Cannot assign this role"
        )
        if "audit.read" in _granted(role.permissions):
            _refuse_audit_read_to_machine(current_user, session)
    person = _accountable_person(session, request.accountable)
    name, email = request.name, f"{request.name}@service.invalid"
    # medic-* is Medic's: the enable flow names and retires accounts of that kind.
    if name.startswith("medic-"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That name is reserved for Medic's account",
        )
    claim_sign_in_names(session, name, email)
    if identifier_in_use(session, name) or identifier_in_use(session, email):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username or email already exists",
        )
    user = User(
        user_id=f"user-{uuid.uuid4().hex[:12]}",
        username=name,
        email=email,
        password_hash=None,
        full_name=name,
        role_id=role.role_id,
        is_active=True,
        is_verified=True,
        service_account=True,
        purpose=request.purpose,
        accountable_principal_id=person.principal_id,
    )
    principals.add_for_user(session, user)
    session.add(user)
    session.flush()
    trail_events.record(
        session,
        "user.created",
        {"personal": trail_events.personal(user)},
        actor=current_user.principal_id,
        principal=user.principal_id,
        role_id=user.role_id,
        service_account=True,
        accountable=person.principal_id,
    )
    logger.info(f"Service account created by {current_user.username}: {name}")
    return _dump_accounts(session, [user])[0]


def _accountable_person(session: Session, user_id: str) -> User:
    """An active person who is not deleted, erased or a service account."""
    person = _not_deleted(session.query(User)).filter(User.user_id == user_id).first()
    if (
        person is None
        or not person.is_active
        or _is_erased(person)
        or not _is_person_account(person, session)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The accountable person must be an active person, not a service account",
        )
    return person


@router.put(
    "/service-accounts/{user_id}/accountable",
    dependencies=[_step_up("users.write")],
)
def reassign_service_account(
    user_id: str,
    body: ReassignRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """Name another person accountable for a service account. users.write over the
    account, and a person's own act; recorded in the trail."""
    with _denials(current_user, "user.reassign_service_account"):
        deny_unless(current_user.user_id, "users.write")
        if not _acts_as_person(current_user, session):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Reassigning needs a person's own sign-in",
            )
        user = _live_user(session, user_id)
        if not _is_service_account(user) or _is_medic(user):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only a service account made on this screen has an accountable person",
            )
        _require_privileges_cover_target(current_user, user, session, action="reassign")
    person = _accountable_person(session, body.accountable)
    previous = user.accountable_principal_id
    user.accountable_principal_id = person.principal_id
    session.flush()
    trail_events.record(
        session,
        "user.accountable_changed",
        actor=current_user.principal_id,
        principal=user.principal_id,
        accountable=person.principal_id,
        previous_accountable=previous,
    )
    who = current_user.full_name or current_user.username
    when_committed(
        session,
        lambda: notify(
            person.principal_id,
            "service_account",
            "You are accountable for a service account",
            f"{who} made you accountable for the service account {user.username}.",
            link="/settings?section=users",
        ),
    )
    logger.info(
        f"Service account reassigned by {current_user.username}: {user.username}"
    )
    return _dump_accounts(session, [user])[0]


def _disable_alone(
    user_id: str, actor: str, by_person: bool
) -> tuple[list[str], list[str]]:
    """Commit a disable on its own: it stands whatever else the request does.
    Returns the credentials and reset links it revoked, for the caller's trail."""
    with unit_of_work() as session:
        user = session.get(User, user_id)
        revoked = deactivate_account(
            session, user, actor=actor, by_person=by_person, record_trail=False
        )
        links = invites.revoke_resets(
            session, user.principal_id, actor, "deactivated", record=False
        )
        return revoked, links


def _apply_user_update(
    session: Session, current_user: User, user_id: str, request: UpdateUserRequest
) -> tuple[User, Optional[str], dict]:
    """Sync half of update_user. Returns the user, what made its existing
    tokens stale (an email or role change, else None), and the payload."""
    deny_unless(current_user.user_id, "users.write")

    user = _live_user(session, user_id)
    _require_privileges_cover_target(current_user, user, session, action="update")

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

    if request.role_id is not None and request.role_id != user.role_id:
        _refuse_lockout(
            current_user,
            session,
            users={user.user_id: (request.role_id, user.is_active)},
        )

    actor, principal = current_user.principal_id, user.principal_id
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

    # Flush so the read-back sees server defaults; the request's unit
    # of work commits, entries included: a trail failure rolls this back.
    session.flush()
    session.refresh(user)
    for action, beside, fields in entries:
        trail_events.record(
            session, action, beside, actor=actor, principal=principal, **fields
        )
    return user, stale_reason, UserSchema.dump(user)


@router.put("/{user_id}", dependencies=[_step_up("users.write")])
async def update_user(
    user_id: str,
    request: UpdateUserRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """
    Update a user's profile and role (requires users.write permission).
    Deactivate, reactivate and restore have their own routes.

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
        # email or role, not the claims and permissions cached at login. In
        # this transaction: the change does not commit without it.
        if stale_reason:
            await asyncio.to_thread(
                sign_ins.end_all_for_user,
                user.user_id,
                sign_ins.ACCOUNT_CHANGED,
                session,
            )

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


def _tell_person(session: Session, user: User, kind: str, title: str, message: str):
    """A notice to the account's holder once ``session`` commits. A service
    account has nobody to tell. Nothing secret: notices are emailed."""
    if _is_service_account(user):
        return
    about = user.principal_id
    when_committed(
        session, lambda: notify(about, kind, title, message, priority="high")
    )


@router.post("/{user_id}/deactivate", dependencies=[_step_up("users.write")])
def deactivate_user(
    user_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """End the account's sign-ins and revoke every credential it holds.
    users.write over the account; a service account or playbook may, as containment."""
    with _denials(current_user, "user.deactivate"):
        deny_unless(current_user.user_id, "users.write")
        user = _live_user(session, user_id)
        _require_privileges_cover_target(
            current_user, user, session, action="deactivate"
        )
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This account is already deactivated",
            )
        _refuse_lockout(
            current_user, session, users={user.user_id: (user.role_id, False)}
        )
    actor, principal = current_user.principal_id, user.principal_id
    by_person = _acts_as_person(current_user, session)
    # A safety action: committed on its own and recorded, or held to record
    # late, so a trail failure leaves it in place.
    if trail_events.ready(session):
        revoked, links = _disable_alone(user_id, actor, by_person)
        trail_events.safety("user.disabled", actor=actor, principal=principal)
        for credential_id in revoked:
            trail_events.safety(
                "credential.revoked",
                actor=actor,
                principal=principal,
                credential_id=credential_id,
            )
        for link_id in links:
            trail_events.safety(
                "reset.revoked",
                actor=actor,
                principal=principal,
                link_id=link_id,
                reason="deactivated",
            )
        session.refresh(user)
    else:
        deactivate_account(session, user, actor=actor, by_person=by_person)
    logger.info(f"User deactivated by {current_user.username}: {user.username}")
    return _dump_accounts(session, [user])[0]


@router.post("/{user_id}/reactivate", dependencies=[_step_up("users.write")])
def reactivate_user(
    user_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """Let a deactivated account sign in again with its password and MFA. Its
    tokens stay revoked. Restores the whole role, so a person's own act."""
    with _denials(current_user, "user.reactivate"):
        deny_unless(current_user.user_id, "users.write")
        user = _live_user(session, user_id)
        _require_privileges_cover_target(
            current_user, user, session, action="reactivate"
        )
        _refuse_erased(user)
        if user.is_active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This account is already active",
            )
        role = session.get(Role, user.role_id)
        _refuse_machine_widening(
            current_user, session, None, role.permissions if role else {}
        )
    user.is_active = True
    # Back in use: the idle clock starts again and a lock ends.
    user.dormancy_floor = utcnow()
    clear_lock(user)
    session.flush()
    trail_events.record(
        session,
        "user.enabled",
        actor=current_user.principal_id,
        principal=user.principal_id,
    )
    # Someone already deactivated at the upgrade has no team. Joining is recorded.
    teams.join_default_if_none(
        session, user.principal_id, actor=current_user.principal_id
    )
    who = current_user.full_name or current_user.username
    _tell_person(
        session,
        user,
        "account_reactivated",
        "Your account was reactivated",
        f"{who} reactivated your account. Sign in with your password and two-step "
        "sign-in as before. MCP tokens from before stay revoked: mint new ones.",
    )
    logger.info(f"User reactivated by {current_user.username}: {user.username}")
    entry = _dump_accounts(session, [user])[0]
    entry["new_tokens_required"] = True
    return entry


@router.post("/{user_id}/restore", dependencies=[_step_up("users.delete")])
def restore_user(
    user_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    session: UnitOfWorkSession,
):
    """Bring a deleted account back, Deactivated: password and MFA kept, tokens
    revoked. users.delete, and a person's own act."""
    with _denials(current_user, "user.restore"):
        deny_unless(current_user.user_id, "users.delete")
        if not _acts_as_person(current_user, session):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Restoring an account needs a person's own sign-in",
            )
        # _live_user skips deleted accounts; this one is looked up as it is.
        user = session.get(User, user_id)
        principal = user and session.get(Principal, user.principal_id)
        if principal is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            )
        _refuse_erased(user)
        if principal.deleted_at is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This account is not deleted",
            )
        _require_privileges_cover_target(current_user, user, session, action="restore")
    actor = current_user.principal_id
    principals.restore(principal)
    user.is_active = False
    session.flush()
    trail_events.record(
        session, "user.restored", actor=actor, principal=user.principal_id
    )
    # Deleting revoked them; one left live (an older delete) is revoked now.
    for credential in mcp_credential_service.list_for_user(
        user.user_id, session=session
    ):
        if mcp_credential_service.revoke(credential.credential_id, session=session):
            trail_events.record(
                session,
                "credential.revoked",
                actor=actor,
                principal=user.principal_id,
                credential_id=credential.credential_id,
            )
    who = current_user.full_name or current_user.username
    _tell_person(
        session,
        user,
        "account_restored",
        "Your account was restored",
        f"{who} restored your deleted account. It stays deactivated until an "
        "administrator reactivates it; MCP tokens from before stay revoked.",
    )
    logger.info(f"User restored by {current_user.username}: {user.username}")
    return _dump_accounts(session, [user])[0]


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
    deactivate_account(
        session,
        user,
        actor=actor,
        by_person=_acts_as_person(current_user, session),
    )
    # In this transaction: the delete does not happen without its entries.
    trail_events.record(
        session, "user.deleted", actor=actor, principal=user.principal_id
    )
    session.flush()
    return user


@router.delete("/{user_id}", dependencies=[_step_up("users.delete")])
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


@router.post(
    "/roles",
    status_code=status.HTTP_201_CREATED,
    dependencies=[_step_up("roles.manage")],
)
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


@router.put("/roles/{role_id}", dependencies=[_step_up("roles.manage")])
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


@router.delete("/roles/{role_id}", dependencies=[_step_up("roles.manage")])
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
