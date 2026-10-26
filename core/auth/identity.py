"""Sign-in identifiers. Login accepts a username or an email, so one string
must name one account: the two columns are unique only within themselves.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func, or_, text
from sqlalchemy.orm import Session

from core.storage.models import User


def claim_sign_in_names(session: Session, *values: str) -> None:
    """Hold these sign-in strings until the caller's transaction ends.

    PostgreSQL only. Two transactions can otherwise each see the string free
    and then commit it on different columns. Other dialects skip the lock;
    the check still runs.
    """
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    for key in sorted({value.casefold() for value in values if value}):
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"vigil-signin:{key}"},
        )


def matching_accounts(session: Session, identifier: str) -> list[User]:
    """Accounts whose username or email is exactly ``identifier``.

    Each column is unique, so this is one account or two. Two means the
    string is not a sign-in name for either of them.
    """
    return (
        session.query(User)
        .filter(or_(User.username == identifier, User.email == identifier))
        .all()
    )


def identifier_in_use(
    session: Session, value: str, except_user_id: Optional[str] = None
) -> bool:
    """Whether another account already uses ``value`` as a username or email.

    Case-insensitive, so ``Ada@Example.com`` collides with ``ada@example.com``.
    An account may use one string for both of its own columns.
    """
    key = value.casefold()
    clauses = [
        or_(
            User.username == value,
            User.email == value,
            func.lower(User.username) == key,
            func.lower(User.email) == key,
        )
    ]
    if except_user_id is not None:
        clauses.append(User.user_id != except_user_id)
    return session.query(User.user_id).filter(*clauses).first() is not None
