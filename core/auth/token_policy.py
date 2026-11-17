"""The token lifetimes of the session policy: what applies, what a save shortens, and the bell.

``personal_token_max_days`` and ``service_token_max_days`` live in the session
policy document (``core.auth.session_policy``). A mint reads them under a shared
lock in its own transaction; a save that lowers one shortens the tokens already
issued in the transaction that holds the exclusive lock, so no token outlives the
maximum in force when it was minted or last lowered. Nothing falls back to the
defaults: a stored policy that cannot be read refuses every mint.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import NamedTuple

from pydantic import ValidationError
from sqlalchemy import func, or_, update
from sqlalchemy.orm import Session

from core.auth import session_policy
from core.auth.session_policy import SessionPolicy
from core.platform import notices
from core.storage.models import McpCredential, User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

UNREADABLE = (
    "The token policy stored on this install can't be read, so no token can be "
    "minted or rotated. Saving Settings › Sessions again fixes it."
)


class PolicyUnreadable(Exception):
    """The stored policy row cannot be parsed; nothing falls back to defaults."""


def current(session: Session) -> SessionPolicy:
    """The policy to mint under, read FOR SHARE until the transaction ends."""
    try:
        return session_policy.read_row(session, lock="share")[0].policy
    except (ValidationError, TypeError, ValueError) as exc:
        raise PolicyUnreadable(UNREADABLE) from exc


def maximum_days(user: User, policy: SessionPolicy) -> int:
    """The most days a token for ``user`` may live."""
    if user.service_account is True:
        return policy.service_token_max_days
    return policy.personal_token_max_days


class Shortening(NamedTuple):
    """Tokens a lowered maximum ends early: the rows, and how many are Medic's or directory sync's."""

    rows: list[tuple[McpCredential, User]]

    @property
    def sync(self) -> int:
        from core.auth.directory_sync import is_sync_account

        return sum(1 for _, holder in self.rows if is_sync_account(holder))

    @property
    def medic(self) -> int:
        from core.auth.service_account import is_medic

        return sum(1 for _, holder in self.rows if is_medic(holder))


def _lowered(old: SessionPolicy, new: SessionPolicy) -> list[tuple[bool, int]]:
    """(service accounts, days) for each maximum ``new`` lowers."""
    return [
        (service, days)
        for service, days, was in (
            (False, new.personal_token_max_days, old.personal_token_max_days),
            (True, new.service_token_max_days, old.service_token_max_days),
        )
        if days < was
    ]


def affected(
    session: Session, old: SessionPolicy, new: SessionPolicy, lock: bool = False
) -> Shortening:
    """Active tokens that end later than ``new`` allows. ``lock`` holds their rows."""
    rows: list[tuple[McpCredential, User]] = []
    for service, days in _lowered(old, new):
        query = (
            session.query(McpCredential, User)
            .join(User, User.user_id == McpCredential.user_id)
            .filter(
                McpCredential.revoked_at.is_(None),
                or_(
                    McpCredential.expires_at.is_(None),
                    McpCredential.expires_at > utcnow() + timedelta(days=days),
                ),
                (
                    (User.service_account.is_(True))
                    if service
                    else (User.service_account.isnot(True))
                ),
            )
            .order_by(McpCredential.created_at)
        )
        rows += (query.with_for_update(of=McpCredential) if lock else query).all()
    return Shortening(rows)


def shorten(
    session: Session, old: SessionPolicy, new: SessionPolicy
) -> tuple[Shortening, dict[str, datetime]]:
    """Cap the active tokens of each lowered kind at now plus its new maximum, in
    one UPDATE each, and say what it did. Never lengthens. Returns the tokens
    and each one's new end. Call with the policy locked, in the saving transaction."""
    found = affected(session, old, new, lock=True)
    ends: dict[str, datetime] = {}
    for service, days in _lowered(old, new):
        end = utcnow() + timedelta(days=days)
        ids = [
            c.credential_id
            for c, h in found.rows
            if (h.service_account is True) == service
        ]
        if ids:
            session.execute(
                update(McpCredential)
                .where(McpCredential.credential_id.in_(ids))
                .values(expires_at=func.least(McpCredential.expires_at, end))
                .execution_options(synchronize_session=False)
            )
            ends.update({i: end for i in ids})
    return found, ends


def unreadable(user: User) -> list[dict[str, str]]:
    """The bell's condition, for a holder of ``sessions.manage``: the stored policy
    cannot be read. One policy read, made only for someone who can fix it."""
    from core.auth.permissions import missing_permission

    if missing_permission(user.user_id, "sessions.manage") is not None:
        return []
    with unit_of_work() as session:
        try:
            session_policy.read_row(session)
        except (ValidationError, TypeError, ValueError):
            return [{"message": UNREADABLE, "link": "/settings?section=sessions"}]
    return []


def register() -> None:
    notices.CONDITIONS.append(unreadable)
