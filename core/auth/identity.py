"""Sign-in identifiers. Login accepts a username or an email, so one string
must name one account: the two columns are unique only within themselves.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import func, or_, text
from sqlalchemy.orm import Session

from core.storage.models import AuthLink, User
from core.time import utcnow


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
    session: Session,
    value: str,
    except_user_id: Optional[str] = None,
    except_link_id: Optional[str] = None,
) -> bool:
    """Whether another account, or a pending invite, already uses ``value`` as
    a username or email.

    Case-insensitive, so ``Ada@Example.com`` collides with ``ada@example.com``.
    An account may use one string for both of its own columns. An invite holds
    its address until it is used, revoked or expired; ``except_link_id`` leaves
    out the invite that is being accepted.
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
    if session.query(User.user_id).filter(*clauses).first() is not None:
        return True
    pending = [
        AuthLink.kind == "invite",
        AuthLink.used_at.is_(None),
        AuthLink.revoked_at.is_(None),
        AuthLink.expires_at > utcnow(),
        func.lower(AuthLink.email) == key,
    ]
    if except_link_id is not None:
        pending.append(AuthLink.link_id != except_link_id)
    return session.query(AuthLink.link_id).filter(*pending).first() is not None
