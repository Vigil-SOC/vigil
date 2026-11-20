"""Sign-in identifiers. Login accepts a username or an email, so one string
must name one account: the two columns are unique only within themselves.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from typing import Optional

from sqlalchemy import func, literal_column, or_, text
from sqlalchemy.orm import Session

from core.auth import key_ring
from core.storage.models import AuthLink, IdentifierReservation, Principal, User
from core.time import utcnow

# What identifier_in_use answers besides None. All truthy, so a caller that only
# asks "is it in use?" refuses every one of them.
IN_USE = "in_use"
RESERVED = "reserved"
# A reservation exists but its key can't be read, so the check can't tell.
UNAVAILABLE = "unavailable"

# Spelled like a principal id: an account's ``user-<12 hex>``, an agent's
# ``agent:<key>``, or the system's ``system``. The trail and the lookups that
# take "username or id" would read such a name as that principal.
_PRINCIPAL_SHAPED = re.compile(r"user-[0-9a-f]{12}|agent:.*|system", re.DOTALL)


def fold(value: str) -> str:
    """The one way identifiers compare: NFKC, then lowercase. Width forms and
    case variants are equal. SQL spells it ``lower(normalize(col, NFKC))``, so
    this lowercases one character at a time and maps ``İ`` to ``i``, as
    PostgreSQL does: ``str.lower`` would add a combining dot and pick Greek final
    sigma by context, and a stored name would then miss its own spelling."""
    return "".join(
        "i" if ch == "\u0130" else ch.lower()
        for ch in unicodedata.normalize("NFKC", value)
    )


def sql_fold(column):
    return func.lower(func.normalize(column, literal_column("NFKC")))


def _digest(key: key_ring.Key, folded: str) -> str:
    return hmac.new(key.secret, folded.encode(), hashlib.sha256).hexdigest()


def reserve(session: Session, *values: Optional[str]) -> None:
    """Keep each distinct folded value as a keyed hash under the current
    ``identifier`` key, dated now; a repeat keeps one row with the later date.
    Raises ``KeyRingError`` when that key can't be read."""
    key = key_ring.current("identifier")
    now = utcnow()
    for folded in sorted({fold(value) for value in values if value}):
        session.execute(
            text("""
                INSERT INTO public.identifier_reservations (hash, key_version, reserved_at)
                VALUES (:hash, :version, :at)
                ON CONFLICT (hash) DO UPDATE SET reserved_at = EXCLUDED.reserved_at
            """),
            {"hash": _digest(key, folded), "version": key.version, "at": now},
        )


def _reserved(session: Session, folded: str) -> Optional[str]:
    """RESERVED, UNAVAILABLE or None. One HMAC per key version in the table,
    so none when it is empty."""
    versions = [
        v for (v,) in session.query(IdentifierReservation.key_version).distinct()
    ]
    hashes, unreadable = [], False
    for version in versions:
        try:
            key = key_ring.find("identifier", version)
        except key_ring.KeyRingError:
            key = None
        if key is None:
            unreadable = True  # never frees it: some reservation can't be matched
        else:
            hashes.append(_digest(key, folded))
    if hashes and (
        session.query(IdentifierReservation.hash)
        .filter(IdentifierReservation.hash.in_(hashes))
        .first()
        is not None
    ):
        return RESERVED
    return UNAVAILABLE if unreadable else None


def claim_sign_in_names(session: Session, *values: str) -> None:
    """Hold these sign-in strings until the caller's transaction ends.

    PostgreSQL only. Two transactions can otherwise each see the string free
    and then commit it on different columns. Other dialects skip the lock;
    the check still runs.
    """
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    for key in sorted({fold(value) for value in values if value}):
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
    *,
    pending_invites: bool = True,
) -> Optional[str]:
    """``IN_USE`` when another account, or a pending invite, uses ``value`` as a
    username or email, ``RESERVED`` when an erased person held it or it is
    (or is spelled like) a principal id, ``UNAVAILABLE``
    when a reservation can't be read, else None.

    Compared by ``fold``, so ``Ada@Example.com`` and the full-width ``ａｄａ@example.com``
    collide with ``ada@example.com``.
    An account may use one string for both of its own columns. An invite holds
    its address until it is used, revoked or expired; ``except_link_id`` leaves
    out the invite that is being accepted. ``pending_invites=False`` asks about
    accounts only, so a route that creates the account can cancel the invite
    instead of treating it as a collision.
    """
    key = fold(value)
    if _names_principal(session, key, except_user_id):
        return RESERVED
    clauses = [
        or_(
            User.username == value,
            User.email == value,
            sql_fold(User.username) == key,
            sql_fold(User.email) == key,
        )
    ]
    if except_user_id is not None:
        clauses.append(User.user_id != except_user_id)
    if session.query(User.user_id).filter(*clauses).first() is not None:
        return IN_USE
    if pending_invites and _invited(session, key, except_link_id):
        return IN_USE
    # An account's own identifier is never held against it by a reservation.
    if except_user_id is not None:
        own = (
            session.query(User.username, User.email)
            .filter(User.user_id == except_user_id)
            .first()
        )
        if own is not None and key in {fold(own.username), fold(own.email)}:
            return None
    # Read order closes the erasure race: accounts and invites above, the
    # reservation last, as its own statement. Erasure rewrites the account and
    # writes the reservation in one transaction, so a check that no longer
    # finds the account sees the reservation. Reading it first would miss one
    # written between. No lock; do not reorder.
    return _reserved(session, key)


def _names_principal(session: Session, key: str, except_user_id: Optional[str]) -> bool:
    """A sign-in name may not be, or look like, a principal id."""
    if _PRINCIPAL_SHAPED.fullmatch(key):
        return True
    query = session.query(Principal.principal_id).filter(
        sql_fold(Principal.principal_id) == key
    )
    if except_user_id is not None:
        query = query.filter(Principal.principal_id != except_user_id)
    return query.first() is not None


def _invited(session: Session, key: str, except_link_id: Optional[str]) -> bool:
    pending = [
        AuthLink.kind == "invite",
        AuthLink.used_at.is_(None),
        AuthLink.revoked_at.is_(None),
        AuthLink.expires_at > utcnow(),
        sql_fold(AuthLink.email) == key,
    ]
    if except_link_id is not None:
        pending.append(AuthLink.link_id != except_link_id)
    return session.query(AuthLink.link_id).filter(*pending).first() is not None
