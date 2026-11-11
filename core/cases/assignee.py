"""An assignee is one active person's username.

Every writer that sets a case's or a case task's assignee, or escalates a
case, resolves what it was given here. Username or email, ignoring case, and
stored as that account's username, so "Assigned to me" (an exact match on the
username) finds it.

A stored owner is judged exactly: current only when it is the username of one
active, non-service person whose account is not deleted. Open work may be saved
only with a current owner or none (#4263).
"""

from typing import Iterable, Literal, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from core.exceptions import SOCError
from core.storage.models import Principal, TeamMembership, User

OwnerState = Literal["current", "not_current"]
CURRENT: OwnerState = "current"
NOT_CURRENT: OwnerState = "not_current"

# A task is open unless done; its status is free text, so open fails safe.
TASK_DONE = ("completed", "cancelled")


def task_is_open(status: Optional[str]) -> bool:
    return status not in TASK_DONE


class AssigneeRefused(SOCError):
    """One message for every refusal, so it says nothing about which accounts exist."""

    status_code = 400

    def __init__(self) -> None:
        super().__init__(
            "Assignee must be the username or email of an active person.",
            "ASSIGNEE_REFUSED",
        )


class OwnerNotCurrent(SOCError):
    """An open save whose resulting owner is not current. One message for every caller."""

    status_code = 400

    def __init__(self, owner: str) -> None:
        super().__init__(
            f"The owner {owner!r} is not a current account: set a current person "
            "or Unassigned in the same update (changing a case owner needs "
            "cases.assign).",
            "OWNER_NOT_CURRENT",
        )


def owner_states(
    session: Optional[Session], owners: Iterable[Optional[str]]
) -> dict[str, OwnerState]:
    """Each non-blank stored owner in ``owners`` to its state, in one query.

    Look a row up with ``.get(owner or "")``: a blank owner (Unassigned) has no
    state. With no session (demo mode, no database) nothing is current.
    """
    names = {o for o in owners if o and o.strip()}
    current: set[str] = set()
    if names and session is not None:
        current = set(
            session.scalars(
                select(User.username)
                .join(Principal, Principal.principal_id == User.principal_id)
                .where(
                    User.username.in_(names),
                    User.is_active.is_(True),
                    User.service_account.is_not(True),
                    Principal.deleted_at.is_(None),
                )
            )
        )
    return {name: CURRENT if name in current else NOT_CURRENT for name in names}


def _team_id(row: dict) -> Optional[str]:
    team = row.get("team")
    if isinstance(team, dict) and isinstance(team.get("id"), str):
        return team["id"]
    team_id = row.get("team_id")
    return team_id if isinstance(team_id, str) else None


def _case_row(row: dict) -> bool:
    """Task rows carry ``task_id``. ``owner_on_team`` is a case fact."""
    return "task_id" not in row


def owners_on_team(
    session: Optional[Session], rows: list[dict], *, key: str = "assignee"
) -> set[tuple[str, str]]:
    """``(username, team_id)`` pairs that are a membership, from one query."""
    names = {
        owner
        for row in rows
        if _case_row(row) and (owner := (row.get(key) or "").strip())
    }
    if not names or session is None:
        return set()
    found = session.execute(
        select(User.username, TeamMembership.team_id)
        .join(TeamMembership, TeamMembership.principal_id == User.principal_id)
        .where(User.username.in_(names))
    ).all()
    return {(username, team_id) for username, team_id in found}


def with_owner_state(
    session: Optional[Session], rows: list[dict], *, key: str = "assignee"
) -> list[dict]:
    """Set ``owner_state`` on each row, and ``owner_on_team`` on case rows.

    ``owner_on_team`` is true or false when the case has an owner, and absent
    when it is unassigned. Task rows are left alone. One membership query.
    """
    states = owner_states(session, (row.get(key) for row in rows))
    on_team = owners_on_team(session, rows, key=key)
    for row in rows:
        row["owner_state"] = states.get(row.get(key) or "")
        if not _case_row(row):
            continue
        owner = (row.get(key) or "").strip()
        if not owner:
            row.pop("owner_on_team", None)
            continue
        row["owner_on_team"] = (owner, _team_id(row)) in on_team
    return rows


def require_current_owner(
    session: Session, owner: Optional[str], *, is_open: bool
) -> None:
    """Refuse a save that leaves work open with a stale owner.

    Judges the resulting owner and status, whether or not the save sent an owner.
    """
    if is_open and owner_states(session, [owner]).get(owner or "") == NOT_CURRENT:
        raise OwnerNotCurrent(owner or "")


def resolve_assignee(
    session: Session, raw: Optional[str], *, current: Optional[str] = None
) -> Optional[str]:
    """The username ``raw`` names, or None for blank (unassign).

    ``current`` is the stored value: re-sending it unchanged keeps it, so an
    edit form that echoes an old assignee back still saves its other fields.
    Raises ``AssigneeRefused`` for no match, more than one match, an inactive
    account, or a service account.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if current is not None and text == current.strip():
        return current
    folded = text.lower()
    # Both columns are unique only case-sensitively, and one person's username
    # can be another's email, so more than one row is a refusal.
    users = session.scalars(
        select(User)
        .where(
            or_(func.lower(User.username) == folded, func.lower(User.email) == folded)
        )
        .limit(2)
    ).all()
    if len(users) != 1:
        raise AssigneeRefused()
    user = users[0]
    if not user.is_active or user.service_account is True:
        raise AssigneeRefused()
    return user.username


def resolve_escalation_target(session: Session, raw: Optional[str]) -> str:
    """As ``resolve_assignee``, but an escalation needs someone: blank is refused."""
    username = resolve_assignee(session, raw)
    if username is None:
        raise AssigneeRefused()
    return username
