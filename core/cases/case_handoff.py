"""Move a case between teams, and claim one that nobody current holds.

Hand back is a ``PATCH`` with an empty assignee. A ``PATCH``, an agent or MCP
``update_case``, or ``create_case`` that sets an owner also refuses a person
who is not a current member of the case's team.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.auth.deactivation import when_committed
from core.auth.permissions import deny_unless, missing_permission, permission_refusal
from core.cases import case_trail
from core.cases.agent_closure import (
    actor_identity,
    close_channel,
    current_credential_id,
    run_agent,
)
from core.cases.assignee import CURRENT, owner_states, resolve_assignee
from core.cases.case_status import CLOSED
from core.cases.closure import CHANNEL_COOKIE
from core.exceptions import NotFoundError, SOCError, TeamRefused
from core.integrations.mcp.surface import current_caller
from core.platform.notices import notify
from core.storage.models import Case, Team, TeamMembership, User
from core.teams import clean_name, default_team
from core.time import utcnow

logger = logging.getLogger(__name__)

ESCALATE_REPLACED = (
    "Escalation has been replaced by the beta move, "
    "POST /api/v1/cases/{case_id}/move, and the move_case tool."
)


class MoveRefused(SOCError):
    """A move or a claim the caller can correct."""

    def __init__(
        self, message: str, status_code: int = 409, code: str = "MOVE_REFUSED"
    ):
        super().__init__(message, code)
        self.status_code = status_code


class OwnerOffTeam(SOCError):
    """The person named as owner is not on the team the case is going to."""

    status_code = 400

    def __init__(self, case_id: str, team_name: str) -> None:
        super().__init__(
            f"That person is not on {team_name}. Move the case with the beta "
            f"operation POST /api/v1/cases/{case_id}/move, or the move_case tool, "
            "which can carry the owner.",
            "OWNER_OFF_TEAM",
        )


class OwnerCannotWrite(SOCError):
    """A person was given a case they cannot edit."""

    status_code = 400

    def __init__(self, username: str) -> None:
        super().__init__(
            f"The owner {username} needs cases.write.",
            "OWNER_CANNOT_WRITE",
        )


def _blank(value: Optional[str]) -> Optional[str]:
    text = (value or "").strip()
    return text or None


def assignee_needs_assign(
    stored: Optional[str], new: Optional[str], caller: str
) -> bool:
    """Whether this owner change needs ``cases.assign``.

    An echo needs nothing extra. Clearing your own needs only ``cases.write``.
    Setting or clearing someone else needs ``cases.assign``.
    """
    before, after = _blank(stored), _blank(new)
    if after == before:
        return False
    return not (after is None and before == _blank(caller))


def newly_set_owner(stored: Optional[str], new: Optional[str]) -> Optional[str]:
    """The username a save newly gives the case, or None for an echo or a clear."""
    before, after = _blank(stored), _blank(new)
    if after and after != before:
        return after
    return None


def require_owner_can_write(session: Session, username: str) -> None:
    """The person being given the case holds ``cases.write`` right now."""
    user = session.scalar(select(User).where(User.username == username))
    if user is None or missing_permission(user.user_id, "cases.write", session):
        raise OwnerCannotWrite(username)


def require_owner_on_team(
    session: Session, username: str, team_id: str, case_id: str
) -> None:
    """The person being given the case is a current member of ``team_id``."""
    if _current_member(session, username, team_id):
        return
    team = session.get(Team, team_id)
    raise OwnerOffTeam(case_id, team.name if team is not None else team_id)


def case_team_id(case: dict) -> Optional[str]:
    """The owning team id on a dumped case, from ``team.id`` or ``team_id``."""
    team = case.get("team")
    if isinstance(team, dict) and isinstance(team.get("id"), str) and team["id"]:
        return team["id"]
    raw = case.get("team_id")
    return raw if isinstance(raw, str) and raw else None


def require_case_owner_on_team(
    session: Session, case: dict, username: str, case_id: str
) -> None:
    """A newly set owner is a current member of the case's team.

    The team is the one on the case. A row with no team id is read from the
    locked case, which always has one after the teams upgrade.
    """
    team_id = case_team_id(case)
    if team_id is None:
        row = session.get(Case, case_id)
        team_id = row.team_id if row is not None else None
    if team_id is None:
        return
    require_owner_on_team(session, username, team_id, case_id)


def _member(session: Session, username: Optional[str], team_id: str) -> bool:
    if not username:
        return False
    principal_id = session.scalar(
        select(User.principal_id).where(User.username == username)
    )
    if principal_id is None:
        return False
    return session.get(TeamMembership, (team_id, principal_id)) is not None


def _current_member(session: Session, username: Optional[str], team_id: str) -> bool:
    name = _blank(username)
    if name is None or owner_states(session, [name]).get(name) != CURRENT:
        return False
    return _member(session, name, team_id)


def _may(
    session: Session, permission: str, *, user_id: Optional[str], http: bool
) -> None:
    """``deny_unless`` on a cookie or bearer call. A bound run uses the ceiling."""
    if http:
        deny_unless(user_id or "", permission, session)
        return
    refusal = permission_refusal(permission)
    if refusal is not None:
        raise MoveRefused(refusal["error"], 403, "PERMISSION_DENIED")


def _lock_team(session: Session, team_id: str) -> Team:
    # populate_existing: a team already loaded in this session (the case's
    # selectin) would otherwise keep a stale archived_at after the lock.
    team = session.get(Team, team_id, with_for_update=True, populate_existing=True)
    if team is None:
        raise TeamRefused("Team not found.", 404)
    return team


def _team_named(session: Session, raw: str) -> Team:
    name = clean_name(raw)
    team_id = session.scalar(
        select(Team.team_id).where(func.lower(Team.name) == name.casefold())
    )
    if team_id is None:
        raise TeamRefused("Team not found.", 404)
    team = _lock_team(session, team_id)
    if team.name.casefold() != name.casefold():
        raise TeamRefused("Team not found.", 404)
    return team


def _refuse_archived(team: Team) -> None:
    if team.archived_at is not None:
        raise TeamRefused(f"{team.name} is archived.", 409)


def _reason(reason: Optional[str]) -> Optional[str]:
    text = (reason or "").strip()
    if run_agent()[1]:
        return text or None
    if not text:
        raise MoveRefused("A move needs a reason.", 400, "MOVE_NEEDS_REASON")
    return text


def _append(
    session: Session,
    action: str,
    case_id: str,
    *,
    operator_text: Optional[str] = None,
    previous: Optional[str] = None,
    new: Optional[str] = None,
    owner_changed: bool = False,
    **fields: object,
) -> None:
    run_id = run_agent()[1]
    credential_id = current_credential_id()
    if run_id:
        fields["run_id"] = run_id
    if credential_id:
        fields["credential_id"] = credential_id

    def holders() -> dict:
        return case_trail.holders(session, previous, new)

    case_trail.append_case(
        session,
        action,
        case_id,
        operator_text=operator_text,
        holders=holders if owner_changed else None,
        **fields,
    )


def note_owner(
    session: Session,
    *,
    case_id: str,
    previous: Optional[str],
    new: Optional[str],
) -> None:
    """After commit, tell a person a cookie session handed them this case.

    A failure is logged and the save stays committed. Skips a claim, a clear,
    an echo, an unowned arrival, a machine, and a bound MCP or agent caller.
    """
    owner = newly_set_owner(previous, new)
    if owner is None or current_caller() is not None or run_agent()[1]:
        return
    if close_channel() != CHANNEL_COOKIE:
        return
    caller = actor_identity(session)[1]
    if owner == _blank(caller):
        return
    user = session.scalar(select(User).where(User.username == owner))
    if user is None:
        return
    principal_id = user.principal_id
    title = f"Case {case_id}"
    message = f"{caller} handed you this case."
    link = f"/cases?case={quote(case_id, safe='')}"

    def send() -> None:
        notify(principal_id, "case_handoff", title, message, link=link)

    when_committed(session, send)


def move_case(
    session: Session,
    case_id: str,
    *,
    source_team_id: str,
    destination_team_id: Optional[str] = None,
    destination_name: Optional[str] = None,
    reason: Optional[str] = None,
    owner: Optional[str] = None,
    username: str,
    user_id: Optional[str],
    http: bool,
):
    """Lock the case, move it, and record one ``case.move`` entry.

    The owner is kept only when they are a current member of the destination.
    Naming someone else needs ``cases.assign``, and that person is on the
    destination and holds ``cases.write``. A closed case is refused.
    """
    case = session.get(Case, case_id, with_for_update=True)
    if case is None:
        raise NotFoundError(f"Case {case_id} not found")
    if case.team_id != source_team_id:
        current = case.team.name if case.team is not None else case.team_id
        raise MoveRefused(f"This case moved to {current}.", 409, "CASE_MOVED")
    if case.status == CLOSED:
        raise MoveRefused("A closed case cannot be moved.", 409, "CASE_CLOSED")
    if not _member(session, username, case.team_id):
        _may(session, "cases.move", user_id=user_id, http=http)

    if destination_team_id:
        destination = _lock_team(session, destination_team_id)
    elif destination_name:
        destination = _team_named(session, destination_name)
    else:
        raise TeamRefused("Team not found.", 404)
    _refuse_archived(destination)

    stated = _reason(reason)
    stored = _blank(case.assignee)
    if owner is not None and owner.strip():
        named = _blank(resolve_assignee(session, owner, current=case.assignee))
        if not _current_member(session, named, destination.team_id):
            raise OwnerOffTeam(case_id, destination.name)
        if newly_set_owner(stored, named):
            _may(session, "cases.assign", user_id=user_id, http=http)
            require_owner_can_write(session, named or "")
        new_owner = named
    elif _current_member(session, stored, destination.team_id):
        new_owner = stored
    else:
        new_owner = None

    previous = stored
    source = case.team
    case.team_id = destination.team_id
    case.assignee = new_owner
    case.team = destination
    case.updated_at = utcnow()
    session.flush()

    changed = previous != new_owner
    texts: list[str] = []
    if stated:
        texts.append(stated)
    if changed:
        texts.extend(case_trail.holder_text(previous, new_owner))
    _append(
        session,
        case_trail.MOVE,
        case_id,
        operator_text="\n".join(texts) or None,
        previous=previous,
        new=new_owner,
        owner_changed=changed,
        from_team=source.team_id if source is not None else source_team_id,
        to_team=destination.team_id,
        from_team_name=source.name if source is not None else None,
        to_team_name=destination.name,
    )
    note_owner(session, case_id=case_id, previous=previous, new=new_owner)
    logger.info("Moved case %s to team %s", case_id, destination.team_id)
    return case


def claim_case(session: Session, case_id: str, user: User):
    """Assign to me: a current member takes an unowned or stale case."""
    if (
        user.service_account
        or owner_states(session, [user.username]).get(user.username) != CURRENT
    ):
        raise MoveRefused("Assign to me is for a current person.", 400, "NOT_A_PERSON")
    case = session.get(Case, case_id, with_for_update=True)
    if case is None:
        raise NotFoundError(f"Case {case_id} not found")
    team_name = case.team.name if case.team is not None else case.team_id
    if not _member(session, user.username, case.team_id):
        raise OwnerOffTeam(case_id, team_name)
    stored = _blank(case.assignee)
    if stored and owner_states(session, [stored]).get(stored) == CURRENT:
        raise MoveRefused(f"Already claimed by {stored}.", 409, "ALREADY_CLAIMED")
    require_owner_can_write(session, user.username)
    case.assignee = user.username
    case.updated_at = utcnow()
    session.flush()
    _append(
        session,
        case_trail.CLAIM,
        case_id,
        operator_text="\n".join(case_trail.holder_text(stored, None)) or None,
        previous=stored,
        new=user.username,
        owner_changed=True,
    )
    logger.info("Claimed case %s", case_id)
    return case


def move_reopened_archived_team(session: Session, case_id: str) -> None:
    """A reopen whose team is archived lands on the default team, owner kept.

    One entry names the reopener. The reason is ``team archived`` and the
    entry is marked automatic. This is the only team change nobody chose.
    """
    case = session.get(Case, case_id, with_for_update=True)
    if case is None:
        return
    source = _lock_team(session, case.team_id)
    if source.archived_at is None:
        return
    destination = _lock_team(session, default_team(session).team_id)
    if source.team_id == destination.team_id:
        return
    case.team_id = destination.team_id
    case.team = destination
    case.updated_at = utcnow()
    session.flush()
    _append(
        session,
        case_trail.REOPEN,
        case_id,
        from_team=source.team_id,
        to_team=destination.team_id,
        from_team_name=source.name,
        to_team_name=destination.name,
        reason="team archived",
        automatic=True,
    )
    logger.info("Reopened case %s onto %s", case_id, destination.team_id)


def require_merge_source(session: Session, username: str, team_id: str) -> None:
    """A cross-team merge needs membership of the source team or ``cases.move``."""
    if _member(session, username, team_id):
        return
    user = session.scalar(select(User).where(User.username == username))
    deny_unless(user.user_id if user is not None else "", "cases.move", session)
