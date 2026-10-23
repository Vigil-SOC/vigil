"""Sign-ins: one id per login, carried as ``sid`` on both tokens through every
refresh, and ended by sign-out, expiry or revocation.

The id is a reference for the record, never a credential: a token is checked
by its signature and ``jti``, and nothing looks a user up by ``sid``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from core.storage.models import SignIn, User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

SIGN_OUT = "sign_out"
EXPIRY = "expiry"
REVOKED = "revoked"


def new_id() -> str:
    return f"si-{uuid.uuid4().hex}"


def start(
    session: Session, sign_in_id: str, principal_id: str, expires_at: Optional[datetime]
) -> SignIn:
    row = SignIn(
        sign_in_id=sign_in_id, principal_id=principal_id, expires_at=expires_at
    )
    session.add(row)
    return row


def end(sign_in_id: str, reason: str, session: Optional[Session] = None) -> bool:
    """End one open sign-in. True if this call ended it.

    For ``EXPIRY`` only once its latest refresh token has run out: an older,
    rotated token expires first and must not end a sign-in still in use.
    """
    with unit_of_work(session) as session:
        row = session.get(SignIn, sign_in_id, with_for_update=True)
        if row is None or row.ended_at is not None:
            return False
        now = utcnow()
        if reason == EXPIRY and (row.expires_at is None or row.expires_at > now):
            return False
        row.ended_at = now
        row.end_reason = reason
        return True


def end_all_for_user(
    user_id: str, reason: str, session: Optional[Session] = None
) -> int:
    """End every open sign-in of that user's principal. Returns how many."""
    with unit_of_work(session) as session:
        principal_id = (
            session.query(User.principal_id).filter(User.user_id == user_id).scalar()
        )
        if principal_id is None:
            return 0
        return (
            session.query(SignIn)
            .filter(SignIn.principal_id == principal_id, SignIn.ended_at.is_(None))
            .update(
                {SignIn.ended_at: utcnow(), SignIn.end_reason: reason},
                synchronize_session=False,
            )
        )
