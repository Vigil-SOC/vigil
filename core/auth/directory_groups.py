"""Pushed groups: the store the SCIM group routes and a user payload share (#4424).

A group is created only by a Group push. A user payload that names an unknown
group does not create one. ``external_id`` is immutable; a display-name change
updates the label only. Members are principal ids of accounts already here.
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from core.audit.retention import is_erased
from core.auth.principals import PERSON
from core.storage.models import (
    DirectoryGroup,
    DirectoryGroupMember,
    Principal,
    SsoLink,
    User,
)

DISPLAY_MAX = 256


def by_id(
    session: Session, group_id: str, lock: bool = False
) -> Optional[DirectoryGroup]:
    if lock:
        return session.get(DirectoryGroup, group_id, with_for_update=True)
    return session.get(DirectoryGroup, group_id)


def by_external(session: Session, external_id: str) -> Optional[DirectoryGroup]:
    return session.scalar(
        select(DirectoryGroup).where(DirectoryGroup.external_id == external_id)
    )


def by_key(session: Session, key: str) -> Optional[DirectoryGroup]:
    """Our id or the provider's externalId."""
    found = by_id(session, key)
    return found if found is not None else by_external(session, key)


def list_groups(session: Session) -> list[DirectoryGroup]:
    return list(
        session.scalars(
            select(DirectoryGroup).order_by(
                DirectoryGroup.display_name, DirectoryGroup.group_id
            )
        )
    )


def create(session: Session, external_id: str, display_name: str) -> DirectoryGroup:
    group = DirectoryGroup(
        group_id=f"group-{uuid.uuid4().hex[:12]}",
        external_id=external_id,
        display_name=display_name,
        version=1,
    )
    session.add(group)
    session.flush()
    return group


def rename(group: DirectoryGroup, display_name: str) -> bool:
    if display_name == group.display_name:
        return False
    group.display_name = display_name
    bump(group)
    return True


def bump(group: DirectoryGroup) -> None:
    group.version = (group.version or 1) + 1


def account(session: Session, issuer: str, value: str) -> Optional[User]:
    """The person ``value`` names: our user id, or the provider's externalId."""
    user = session.get(User, value)
    if user is not None and _usable(session, user):
        return user
    link = (
        session.query(SsoLink)
        .filter(
            SsoLink.issuer == issuer,
            or_(SsoLink.directory_id == value, SsoLink.subject == value),
        )
        .first()
    )
    if link is None:
        return None
    user = (
        session.query(User).filter(User.principal_id == link.principal_id).one_or_none()
    )
    if user is None or not _usable(session, user):
        return None
    return user


def _usable(session: Session, user: User) -> bool:
    principal = session.get(Principal, user.principal_id)
    if (
        principal is None
        or principal.deleted_at is not None
        or principal.kind != PERSON
        or user.service_account
        or is_erased(user.username, user.principal_id)
    ):
        return False
    return True


def member_users(session: Session, group_id: str) -> list[User]:
    return list(
        session.scalars(
            select(User)
            .join(
                DirectoryGroupMember,
                DirectoryGroupMember.principal_id == User.principal_id,
            )
            .where(DirectoryGroupMember.group_id == group_id)
            .order_by(User.username)
        )
    )


def principal_ids(session: Session, group_id: str) -> set[str]:
    return set(
        session.scalars(
            select(DirectoryGroupMember.principal_id).where(
                DirectoryGroupMember.group_id == group_id
            )
        )
    )


def external_ids_for(
    session: Session, principal_ids_: list[str]
) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {pid: set() for pid in principal_ids_}
    if not principal_ids_:
        return found
    rows = session.execute(
        select(DirectoryGroupMember.principal_id, DirectoryGroup.external_id)
        .join(DirectoryGroup, DirectoryGroup.group_id == DirectoryGroupMember.group_id)
        .where(DirectoryGroupMember.principal_id.in_(principal_ids_))
    ).all()
    for principal_id, external_id in rows:
        found.setdefault(principal_id, set()).add(external_id)
    return found


def _lock_group(session: Session, group: DirectoryGroup) -> DirectoryGroup:
    locked = by_id(session, group.group_id, lock=True)
    return locked if locked is not None else group


def _has(session: Session, group_id: str, principal_id: str) -> bool:
    return session.get(DirectoryGroupMember, (group_id, principal_id)) is not None


def add_principal(session: Session, group: DirectoryGroup, principal_id: str) -> bool:
    group = _lock_group(session, group)
    if _has(session, group.group_id, principal_id):
        return False
    session.add(
        DirectoryGroupMember(group_id=group.group_id, principal_id=principal_id)
    )
    bump(group)
    session.flush()
    return True


def remove_principal(
    session: Session, group: DirectoryGroup, principal_id: str
) -> bool:
    group = _lock_group(session, group)
    row = session.get(DirectoryGroupMember, (group.group_id, principal_id))
    if row is None:
        return False
    session.delete(row)
    bump(group)
    session.flush()
    return True


def replace_principals(
    session: Session, group: DirectoryGroup, principal_ids_: set[str]
) -> set[str]:
    """Membership becomes ``principal_ids_``. Returns every principal that moved."""
    group = _lock_group(session, group)
    current = principal_ids(session, group.group_id)
    removed = current - principal_ids_
    added = principal_ids_ - current
    for principal_id in removed:
        row = session.get(DirectoryGroupMember, (group.group_id, principal_id))
        if row is not None:
            session.delete(row)
    for principal_id in added:
        session.add(
            DirectoryGroupMember(group_id=group.group_id, principal_id=principal_id)
        )
    if removed or added:
        bump(group)
        session.flush()
    return removed | added


def delete(session: Session, group: DirectoryGroup) -> set[str]:
    """Drop the group and its members. Returns the principals that were on it."""
    group = _lock_group(session, group)
    held = principal_ids(session, group.group_id)
    session.delete(group)
    session.flush()
    return held


def known_external_ids(session: Session) -> set[str]:
    return set(session.scalars(select(DirectoryGroup.external_id)))


def apply_user_groups(
    session: Session,
    principal_id: str,
    *,
    add: list[str],
    remove: list[str],
    replace: Optional[list[str]],
) -> tuple[list[str], bool]:
    """Point this person at groups that already exist.

    ``replace`` is the full set when a user push says so. Unknown keys are
    returned and not created. The bool is whether a membership moved.
    """
    unknown: list[str] = []
    changed = False
    if replace is not None:
        desired: set[str] = set()
        for key in replace:
            group = by_key(session, key)
            if group is None:
                unknown.append(key)
            else:
                desired.add(group.group_id)
        current = set(
            session.scalars(
                select(DirectoryGroupMember.group_id).where(
                    DirectoryGroupMember.principal_id == principal_id
                )
            )
        )
        for group_id in current - desired:
            group = by_id(session, group_id, lock=True)
            if group is not None and remove_principal(session, group, principal_id):
                changed = True
        for group_id in desired - current:
            group = by_id(session, group_id, lock=True)
            if group is not None and add_principal(session, group, principal_id):
                changed = True
    for key in add:
        group = by_key(session, key)
        if group is None:
            unknown.append(key)
            continue
        if add_principal(session, group, principal_id):
            changed = True
    for key in remove:
        group = by_key(session, key)
        if group is None:
            unknown.append(key)
            continue
        if remove_principal(session, group, principal_id):
            changed = True
    return unknown, changed
