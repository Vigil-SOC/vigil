"""Teams and who is on them.

Membership routes work. It grants nothing, except reading the restricted cases
that name the team (#4360): a join to such a team is judged by ``may_grant``.
The default team is the one row with ``is_default``, never a name looked up.
A membership is a person principal.
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.audit.trail import SYSTEM
from core.auth import trail_events
from core.auth.principals import PERSON
from core.exceptions import TeamRefused
from core.storage.models import (
    AuthLink,
    Case,
    Principal,
    Team,
    TeamMembership,
    User,
)
from core.time import utcnow

NAME_MAX = 100
# Never names a case: a refused actor learns nothing about what it can't read.
GRANTS_ACCESS = "Adding to this team grants access you don't hold."
PERSON_MUST = "A person must do this: adding to this team grants access."


def clean_name(raw: str) -> str:
    name = " ".join((raw or "").split())
    if not name:
        raise TeamRefused("A team needs a name.", 400)
    if len(name) > NAME_MAX:
        raise TeamRefused(f"A team name can be at most {NAME_MAX} characters.", 400)
    return name


def default_team(session: Session) -> Team:
    team = session.scalars(select(Team).where(Team.is_default.is_(True))).first()
    if team is None:
        raise TeamRefused("There is no default team.", 500)
    return team


def list_teams(session: Session) -> list[Team]:
    return list(
        session.scalars(select(Team).order_by(func.lower(Team.name), Team.team_id))
    )


def members_of(session: Session, team_id: str) -> list[User]:
    return list(
        session.scalars(
            select(User)
            .join(TeamMembership, TeamMembership.principal_id == User.principal_id)
            .where(TeamMembership.team_id == team_id)
            .order_by(func.lower(User.username))
        )
    )


def teams_for(session: Session, usernames: set[str]) -> dict[str, list[Team]]:
    """Non-archived teams for each username, from one query."""
    found: dict[str, list[Team]] = {name: [] for name in usernames}
    if not usernames:
        return found
    rows = session.execute(
        select(User.username, Team)
        .join(TeamMembership, TeamMembership.principal_id == User.principal_id)
        .join(Team, Team.team_id == TeamMembership.team_id)
        .where(User.username.in_(usernames), Team.archived_at.is_(None))
        .order_by(func.lower(Team.name))
    ).all()
    for username, team in rows:
        found.setdefault(username, []).append(team)
    return found


def create_team(session: Session, name: str, *, actor: str) -> Team:
    name = clean_name(name)
    _refuse_taken(session, name)
    team = Team(
        team_id=f"team-{uuid.uuid4().hex[:12]}",
        name=name,
        is_default=False,
    )
    session.add(team)
    _flush_name(session, name)
    _record(session, "team.created", actor, team_id=team.team_id, name=name)
    return team


def rename_team(session: Session, team_id: str, name: str, *, actor: str) -> Team:
    team = _lock(session, team_id)
    name = clean_name(name)
    if name == team.name:
        return team
    _refuse_taken(session, name, except_id=team.team_id)
    previous = team.name
    team.name = name
    _flush_name(session, name)
    _record(
        session,
        "team.renamed",
        actor,
        team_id=team.team_id,
        name=name,
        previous_name=previous,
    )
    return team


def archive_team(session: Session, team_id: str, *, actor: str) -> Team:
    """Archive only when every case on the team is closed, in this transaction."""
    team = _lock(session, team_id)
    if team.is_default:
        raise TeamRefused("The default team cannot be archived.", 409)
    if team.archived_at is not None:
        raise TeamRefused("This team is already archived.", 409)
    open_cases = session.scalar(
        select(func.count())
        .select_from(Case)
        .where(Case.team_id == team.team_id, Case.status != "closed")
    )
    if open_cases:
        noun = "case" if open_cases == 1 else "cases"
        raise TeamRefused(f"This team has {open_cases} open {noun}.", 409)
    team.archived_at = utcnow()
    session.flush()
    _record(session, "team.archived", actor, team_id=team.team_id)
    return team


def unarchive_team(session: Session, team_id: str, *, actor: str) -> Team:
    team = _lock(session, team_id)
    if team.archived_at is None:
        raise TeamRefused("This team is not archived.", 409)
    team.archived_at = None
    session.flush()
    _record(session, "team.unarchived", actor, team_id=team.team_id)
    return team


def add_member(
    session: Session, team_id: str, user_id: str, *, actor: str, direct: bool
) -> User:
    """``direct``: a person acting in their own session, which a join to a team
    a restricted case names needs."""
    team = _lock(session, team_id)
    _refuse_archived(team)
    user = _person(session, user_id)
    if _has(session, team.team_id, user.principal_id):
        raise TeamRefused("This person is already on the team.", 409)
    if not direct and _named(session, team.team_id):
        raise TeamRefused(PERSON_MUST, 403)
    _insert(session, team, user.principal_id, actor)
    return user


def remove_member(
    session: Session, team_id: str, user_id: str, *, actor: str
) -> Optional[str]:
    """Remove the membership. The warning names a last team, or a team left empty."""
    team = _lock(session, team_id)
    user = session.get(User, user_id)
    if user is None:
        raise TeamRefused("User not found.", 404)
    if not _has(session, team.team_id, user.principal_id):
        raise TeamRefused("This person is not on the team.", 404)
    session.delete(session.get(TeamMembership, (team.team_id, user.principal_id)))
    session.flush()
    _record(
        session,
        "team.member_removed",
        actor,
        team_id=team.team_id,
        principal=user.principal_id,
    )
    return _removal_warning(session, team, user.principal_id)


def check_invite_teams(
    session: Session, team_ids: Optional[list[str]], *, actor: str, direct: bool
) -> list[str]:
    """The ids an invite may store: each exists, is not archived, and its
    issuer ``actor`` may grant what the team reads (``direct`` as add_member)."""
    chosen: list[str] = []
    seen: set[str] = set()
    for raw in team_ids or []:
        team_id = (raw or "").strip()
        if not team_id or team_id in seen:
            continue
        seen.add(team_id)
        chosen.append(team_id)
    if not chosen:
        return []
    # Lock in id order so two invites cannot deadlock each other.
    locked = {
        team.team_id: team for team in (_lock(session, tid) for tid in sorted(chosen))
    }
    out: list[str] = []
    for team_id in chosen:
        team = locked[team_id]
        _refuse_archived(team)
        _refuse_grant(session, team.team_id, actor, direct)
        out.append(team.team_id)
    return out


def _named(session: Session, team_id: str) -> list:
    """The reader lists of every restricted case naming ``team_id``."""
    return list(
        session.execute(
            select(Case.reader_principal_ids, Case.reader_team_ids).where(
                Case.reader_team_ids.any(team_id)
            )
        ).all()
    )


def may_grant(session: Session, team_id: str, actor: Optional[str]) -> bool:
    """The join test, once: a team no restricted case names is open to any join;
    else the actor is an active person who can read every case naming it."""
    from core.auth.permissions import case_reader_for, may_read_case

    cases = _named(session, team_id)
    if not cases:
        return True
    principal = session.get(Principal, actor) if actor else None
    if (
        principal is None
        or principal.deleted_at is not None
        or principal.kind != PERSON
    ):
        return False
    user = session.scalar(select(User).where(User.principal_id == actor))
    if user is None or not user.is_active or user.service_account:
        return False
    reader = case_reader_for(actor, session)
    return all(may_read_case(case, reader) for case in cases)


def _refuse_grant(
    session: Session, team_id: str, actor: Optional[str], direct: bool = True
) -> None:
    if not direct and _named(session, team_id):
        raise TeamRefused(PERSON_MUST, 403)
    if not may_grant(session, team_id, actor):
        raise TeamRefused(GRANTS_ACCESS, 403)


def join_default(session: Session, principal_id: str, *, actor: str) -> None:
    """Put a new person on the default team. A second call changes nothing."""
    _insert(session, default_team(session), principal_id, actor)


def join_named(
    session: Session, principal_id: str, team_ids: list[str], *, actor: str
) -> None:
    """The teams an invite named, instead of the default team.

    Each join is judged as the invite's issuer, now: one the issuer may no
    longer grant is skipped and recorded, and the rest go ahead. When every one
    is skipped the person joins the default team instead.
    """
    teams = [_lock(session, team_id) for team_id in sorted(set(team_ids))]
    by_id = {team.team_id: team for team in teams}
    joined = False
    for team_id in team_ids:
        team = by_id[team_id]
        _refuse_archived(team)
        if not may_grant(session, team.team_id, actor):
            _record(
                session,
                "team.join_skipped",
                actor,
                team_id=team.team_id,
                principal=principal_id,
            )
            continue
        _insert(session, team, principal_id, actor)
        joined = True
    if not joined:
        join_default(session, principal_id, actor=SYSTEM)
        _record(
            session,
            "team.join_defaulted",
            actor,
            team_id=default_team(session).team_id,
            principal=principal_id,
        )


def join_for_new_account(
    session: Session, principal_id: str, invite_link_id: Optional[str]
) -> None:
    """Default team, or the teams stored on the invite being accepted."""
    ids: list[str] = []
    actor = SYSTEM
    if invite_link_id:
        link = session.get(AuthLink, invite_link_id)
        if link is not None and link.team_ids:
            ids = [team_id for team_id in link.team_ids if team_id]
            if ids and link.invited_by:
                actor = link.invited_by
    if ids:
        join_named(session, principal_id, ids, actor=actor)
    else:
        join_default(session, principal_id, actor=SYSTEM)


def join_default_if_none(session: Session, principal_id: str, *, actor: str) -> None:
    """A person with no team, brought back, joins the default team.

    A service account is not a person and never joins, so this is a no-op for it.
    """
    principal = session.get(Principal, principal_id)
    if (
        principal is None
        or principal.deleted_at is not None
        or principal.kind != PERSON
    ):
        return
    held = session.scalar(
        select(TeamMembership.team_id)
        .where(TeamMembership.principal_id == principal_id)
        .limit(1)
    )
    if held is None:
        join_default(session, principal_id, actor=actor)


def _lock(session: Session, team_id: str) -> Team:
    team = session.get(Team, team_id, with_for_update=True)
    if team is None:
        raise TeamRefused("Team not found.", 404)
    return team


def _refuse_archived(team: Team) -> None:
    if team.archived_at is not None:
        raise TeamRefused(f"{team.name} is archived.", 409)


def _refuse_taken(session: Session, name: str, except_id: Optional[str] = None) -> None:
    stmt = select(Team.team_id).where(func.lower(Team.name) == name.casefold())
    if except_id is not None:
        stmt = stmt.where(Team.team_id != except_id)
    if session.scalar(stmt.limit(1)) is not None:
        raise TeamRefused(f"A team named {name} already exists.", 409)


def _flush_name(session: Session, name: str) -> None:
    try:
        session.flush()
    except IntegrityError as exc:
        if "uq_teams_name" not in str(getattr(exc, "orig", exc)):
            raise
        raise TeamRefused(f"A team named {name} already exists.", 409) from exc


def _person(session: Session, user_id: str) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise TeamRefused("User not found.", 404)
    principal = session.get(Principal, user.principal_id)
    if (
        principal is None
        or principal.deleted_at is not None
        or principal.kind != PERSON
        or user.service_account
    ):
        raise TeamRefused("Only a person can be on a team.", 400)
    return user


def _has(session: Session, team_id: str, principal_id: str) -> bool:
    return session.get(TeamMembership, (team_id, principal_id)) is not None


def _insert(session: Session, team: Team, principal_id: str, actor: str) -> None:
    if team.archived_at is not None:
        raise TeamRefused(f"{team.name} is archived.", 409)
    if _has(session, team.team_id, principal_id):
        return
    if not may_grant(session, team.team_id, actor):
        raise TeamRefused(GRANTS_ACCESS, 403)
    principal = session.get(Principal, principal_id)
    if (
        principal is None
        or principal.deleted_at is not None
        or principal.kind != PERSON
    ):
        raise TeamRefused("Only a person can be on a team.", 400)
    session.add(TeamMembership(team_id=team.team_id, principal_id=principal_id))
    session.flush()
    _record(
        session,
        "team.member_added",
        actor,
        team_id=team.team_id,
        principal=principal_id,
    )


def _removal_warning(session: Session, team: Team, principal_id: str) -> Optional[str]:
    parts: list[str] = []
    left = session.scalar(
        select(func.count())
        .select_from(TeamMembership)
        .where(TeamMembership.principal_id == principal_id)
    )
    if not left:
        parts.append("They are on no team, so they can claim nothing.")
    members = session.scalar(
        select(func.count())
        .select_from(TeamMembership)
        .where(TeamMembership.team_id == team.team_id)
    )
    if not members:
        unowned = session.scalar(
            select(func.count())
            .select_from(Case)
            .where(
                Case.team_id == team.team_id,
                Case.status != "closed",
                or_(Case.assignee.is_(None), Case.assignee == ""),
            )
        )
        if unowned:
            noun = "case" if unowned == 1 else "cases"
            parts.append(f"This team has {unowned} unowned {noun} and no members.")
    return " ".join(parts) or None


def _record(session: Session, action: str, actor: str, **fields: object) -> None:
    trail_events.record(session, action, actor=actor, **fields)
