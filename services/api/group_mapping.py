"""Apply a saved group mapping to managed accounts (#4424).

A raise is a widening the mapping's author can still assign. Anything else,
including a move down onto the default role, is applied. A change that would
leave no role manager is skipped. Team joins go through ``join_named`` as that
author; leaves go through ``remove_member`` and do not trip the hand-edit refusal.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from core import teams
from core.auth import directory_groups, sso_settings, trail_events
from core.auth.principals import PERSON
from core.exceptions import TeamRefused
from core.storage.models import Principal, Role, SsoLink, Team, TeamMembership, User
from services.api.routers.auth import _end_others_after_change
from services.api.routers.users import (
    _can_assign_role,
    _granted,
    _leaves_no_manager,
)

NO_AUTHOR = (
    "No mapping has been saved, so a group does not raise a role "
    "or add anyone to a team."
)
AUTHOR_GONE = (
    "Save the mapping again. The person who saved it is missing or inactive, "
    "so a group does not raise a role or add anyone to a team."
)
DEFAULT_TEAM = "The default team cannot be mapped: everyone joins it."


@dataclass
class Effect:
    roles_rise: int = 0
    roles_fall: int = 0
    roles_default: int = 0
    memberships_added: int = 0
    memberships_removed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "roles_rise": self.roles_rise,
            "roles_fall": self.roles_fall,
            "roles_default": self.roles_default,
            "memberships_added": self.memberships_added,
            "memberships_removed": self.memberships_removed,
        }


def author_of(session: Session, settings: sso_settings.SsoSettings) -> Optional[User]:
    """The saved author, when that person is still active."""
    if not settings.mapping_author:
        return None
    user = session.get(User, settings.mapping_author)
    if user is None or not user.is_active or user.service_account:
        return None
    principal = session.get(Principal, user.principal_id)
    if (
        principal is None
        or principal.deleted_at is not None
        or principal.kind != PERSON
    ):
        return None
    return user


def mapping_note(session: Session, settings: sso_settings.SsoSettings) -> Optional[str]:
    if not settings.sync_enabled:
        return None
    if not settings.mapping_author:
        return NO_AUTHOR
    if author_of(session, settings) is None:
        return AUTHOR_GONE
    return None


def broken_lines(session: Session, settings: sso_settings.SsoSettings) -> list[dict]:
    """Stored external ids with no group row. Only while sync is on: off, the
    role lines are claim strings."""
    if not settings.sync_enabled:
        return []
    known = directory_groups.known_external_ids(session)
    found = []
    seen: set[tuple[str, str]] = set()
    for line in settings.group_roles:
        key = (line.group, "role")
        if line.group not in known and key not in seen:
            seen.add(key)
            found.append({"external_id": line.group, "on": "role"})
    for line in settings.team_lines:
        key = (line.group, "team")
        if line.group not in known and key not in seen:
            seen.add(key)
            found.append({"external_id": line.group, "on": "team"})
    return found


def skipped_joins(session: Session) -> int:
    if not trail_events.ready(session):
        return 0
    row = session.execute(text("""
            SELECT count(*) FROM public.audit_trail
            WHERE payload->>'action' = 'team.join_skipped'
              AND payload->>'via' = 'directory'
            """)).first()
    return int(row[0] if row else 0)


def role_refusals(session: Session, settings: sso_settings.SsoSettings) -> list[str]:
    """Usernames a mapping left on a role it would still change.

    Listed until the role the mapping gives them is the role they hold, not only
    while they are the last role manager: the floor lifting applies nothing.
    """
    if not trail_events.ready(session):
        return []
    rows = session.execute(text("""
            SELECT payload->>'user_id' FROM public.audit_trail
            WHERE payload->>'action' = 'directory.role_skipped'
              AND payload->>'reason' = 'last_manager'
            ORDER BY seq DESC LIMIT 200
            """)).all()
    seen: list[str] = []
    for (user_id,) in rows:
        if not user_id or user_id in seen:
            continue
        seen.append(user_id)
    held = [u for u in (session.get(User, i) for i in seen) if u and u.is_active]
    managed = {
        u.user_id
        for u in _managed(session, settings.issuer, {u.principal_id for u in held})
    }
    groups = directory_groups.external_ids_for(session, [u.principal_id for u in held])
    return [
        user.username
        for user in held
        if user.user_id in managed
        and _mapped_role(settings, groups.get(user.principal_id, set()))[1]
        != user.role_id
    ]


def names_default_team(session: Session, lines: list[sso_settings.TeamLine]) -> bool:
    if not lines:
        return False
    default = teams.default_team(session)
    return any(line.team_id == default.team_id for line in lines)


def apply_current(
    session: Session,
    settings: sso_settings.SsoSettings,
    principals: Optional[set[str]],
    version: int,
) -> Effect:
    """Apply the stored mapping. No author: store only, change nothing."""
    actor = author_of(session, settings)
    if actor is None or not settings.sync_enabled:
        return Effect()
    return reconcile(
        session, settings, actor, principals=principals, version=version, write=True
    )


def reconcile(
    session: Session,
    settings: sso_settings.SsoSettings,
    actor: User,
    *,
    principals: Optional[set[str]],
    version: int,
    write: bool,
) -> Effect:
    """The roles and memberships ``settings`` would apply now, as ``actor``.

    ``write`` false counts and changes nothing. ``principals`` none is every
    managed account; a set limits the pass to those principals.
    """
    effect = Effect()
    users = _managed(session, settings.issuer, principals)
    if not users:
        return effect
    groups = directory_groups.external_ids_for(
        session, [user.principal_id for user in users]
    )
    held = _memberships(session, [user.principal_id for user in users])
    mapped = {line.team_id for line in settings.team_lines}
    pending: dict[str, tuple[str, bool]] = {}
    # Same order on a count and on the write, so a last-manager check agrees.
    for user in sorted(users, key=lambda row: row.user_id):
        _one_role(
            session,
            settings,
            actor,
            user,
            groups.get(user.principal_id, set()),
            pending,
            version,
            write,
            effect,
        )
        _one_teams(
            session,
            settings,
            actor,
            user,
            groups.get(user.principal_id, set()),
            held.get(user.principal_id, set()),
            mapped,
            write,
            effect,
        )
    return effect


def _managed(
    session: Session, issuer: Optional[str], principals: Optional[set[str]]
) -> list[User]:
    if not issuer:
        return []
    query = (
        session.query(User)
        .join(SsoLink, SsoLink.principal_id == User.principal_id)
        .join(Principal, Principal.principal_id == User.principal_id)
        .filter(
            SsoLink.issuer == issuer,
            SsoLink.managed.is_(True),
            Principal.deleted_at.is_(None),
            Principal.kind == PERSON,
            User.service_account.is_(False),
        )
    )
    if principals is not None:
        if not principals:
            return []
        query = query.filter(User.principal_id.in_(principals))
    return query.all()


def _memberships(session: Session, principal_ids: list[str]) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {pid: set() for pid in principal_ids}
    if not principal_ids:
        return found
    rows = session.execute(
        select(TeamMembership.principal_id, TeamMembership.team_id).where(
            TeamMembership.principal_id.in_(principal_ids)
        )
    ).all()
    for principal_id, team_id in rows:
        found.setdefault(principal_id, set()).add(team_id)
    return found


def _mapped_role(
    settings: sso_settings.SsoSettings, external_ids: set[str]
) -> tuple[Optional[str], str]:
    """The first mapped group the person is in, and the role it gives."""
    for line in settings.group_roles:
        if line.group in external_ids:
            return line.group, line.role_id
    return None, settings.default_role_id


def _one_role(
    session: Session,
    settings: sso_settings.SsoSettings,
    actor: User,
    user: User,
    external_ids: set[str],
    pending: dict[str, tuple[str, bool]],
    version: int,
    write: bool,
    effect: Effect,
) -> None:
    matched, role_id = _mapped_role(settings, external_ids)
    if role_id == user.role_id:
        return
    role = session.get(Role, role_id)
    if role is None:
        if write:
            _skip_role(session, actor, user, role_id, "role_missing")
        return
    previous = session.get(Role, user.role_id)
    before = _granted(previous.permissions if previous else None)
    after = _granted(role.permissions)
    widening = bool(after - before)
    onto_default = matched is None and role_id == settings.default_role_id
    if widening and not _can_assign_role(actor, role, session):
        if write:
            _skip_role(session, actor, user, role_id, "cannot_assign")
        return
    users = dict(pending)
    users[user.user_id] = (role_id, user.is_active)
    if _leaves_no_manager(session, users=users):
        if write:
            _skip_role(session, actor, user, role_id, "last_manager")
        return
    pending[user.user_id] = (role_id, user.is_active)
    if onto_default:
        effect.roles_default += 1
    elif widening:
        effect.roles_rise += 1
    else:
        effect.roles_fall += 1
    if not write:
        return
    trail_events.record(
        session,
        "user.role_changed",
        actor=actor.principal_id,
        principal=user.principal_id,
        role_id=role.role_id,
        previous_role_id=user.role_id,
        permissions_before=sorted(before),
        permissions_after=sorted(after),
        via="directory",
        version=version,
        group=matched,
    )
    user.role_id = role.role_id
    _end_others_after_change(session, user, None, None)


def _skip_role(
    session: Session, actor: User, user: User, role_id: str, reason: str
) -> None:
    trail_events.record(
        session,
        "directory.role_skipped",
        actor=actor.principal_id,
        principal=user.principal_id,
        user_id=user.user_id,
        username=user.username,
        role_id=role_id,
        reason=reason,
    )


def _one_teams(
    session: Session,
    settings: sso_settings.SsoSettings,
    actor: User,
    user: User,
    external_ids: set[str],
    current: set[str],
    mapped: set[str],
    write: bool,
    effect: Effect,
) -> None:
    desired: list[str] = []
    seen: set[str] = set()
    for line in settings.team_lines:
        if line.group in external_ids and line.team_id not in seen:
            seen.add(line.team_id)
            desired.append(line.team_id)
    desired_set = set(desired)
    for team_id in sorted(current & mapped - desired_set):
        if write:
            try:
                teams.remove_member(
                    session,
                    team_id,
                    user.user_id,
                    actor=actor.principal_id,
                    by_mapping=True,
                )
            except TeamRefused:
                trail_events.record(
                    session,
                    "directory.leave_skipped",
                    actor=actor.principal_id,
                    principal=user.principal_id,
                    team_id=team_id,
                )
                continue
        effect.memberships_removed += 1
    to_join = [team_id for team_id in desired if team_id not in current]
    viable: list[str] = []
    skipped: list[str] = []
    for team_id in to_join:
        team = session.get(Team, team_id)
        if (
            team is None
            or team.archived_at is not None
            or not teams.may_grant(session, team_id, actor.principal_id)
        ):
            skipped.append(team_id)
        else:
            viable.append(team_id)
    effect.memberships_added += len(viable)
    if to_join and not viable:
        default = teams.default_team(session)
        if default.team_id not in current:
            effect.memberships_added += 1
    if not write:
        return
    for team_id in skipped:
        trail_events.record(
            session,
            "team.join_skipped",
            actor=actor.principal_id,
            team_id=team_id,
            principal=user.principal_id,
            via="directory",
        )
    if viable:
        try:
            teams.join_named(
                session,
                user.principal_id,
                viable,
                actor=actor.principal_id,
                via="directory",
            )
        except TeamRefused:
            # A team vanished between the check and the join. The rest of the
            # push stands; this person is not joined to a team we cannot see.
            for team_id in viable:
                trail_events.record(
                    session,
                    "team.join_skipped",
                    actor=actor.principal_id,
                    team_id=team_id,
                    principal=user.principal_id,
                    via="directory",
                )
    elif to_join:
        teams.join_default(session, user.principal_id, actor=actor.principal_id)
        trail_events.record(
            session,
            "team.join_defaulted",
            actor=actor.principal_id,
            team_id=teams.default_team(session).team_id,
            principal=user.principal_id,
            via="directory",
        )
