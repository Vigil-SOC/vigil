"""An assignee is one active person's username.

Every writer that sets a case's or a case task's assignee, or escalates a
case, resolves what it was given here. Username or email, ignoring case, and
stored as that account's username, so "Assigned to me" (an exact match on the
username) finds it. Rows already stored are left as they are.
"""

from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from core.exceptions import SOCError
from core.storage.models import User


class AssigneeRefused(SOCError):
    """One message for every refusal, so it says nothing about which accounts exist."""

    status_code = 400

    def __init__(self) -> None:
        super().__init__(
            "Assignee must be the username or email of an active person.",
            "ASSIGNEE_REFUSED",
        )


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
