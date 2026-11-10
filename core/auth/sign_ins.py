"""Sign-ins: one row per login, and the only record of whether a token is good.

The id rides as ``sid`` on both tokens through every refresh. A token is
honoured only while its row is open and inside the session policy
(``core/auth/session_policy.py``), so ending the row refuses it at the next
request whatever the browser holds.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from core.storage.models import Principal, SignIn, User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

# End reasons. 'revoked' stays valid in the table's check for rows that
# already carry it; nothing ends for it now.
SIGN_OUT = "sign_out"
EXPIRY = "expiry"
IDLE = "idle"
LENGTH_LIMIT = "length_limit"
TOKEN_REUSE = "token_reuse"
UPGRADE = "upgrade"
# Role, password or email changed, or a password reset: sign in again.
ACCOUNT_CHANGED = "account_changed"
DEACTIVATED = "deactivated"
ERASED = "erased"
# A newer sign-in took this one's place under the account's sign-in limit.
SIGN_IN_LIMIT = "sign_in_limit"

# A person's own action stamps the row at most this often.
STAMP_EVERY = timedelta(seconds=30)
# A refresh token one generation behind still works this long after a rotation:
# a second tab refreshing at the same moment.
REFRESH_GRACE = timedelta(seconds=10)


def new_id() -> str:
    return f"si-{uuid.uuid4().hex}"


def device_of(user_agent: Optional[str]) -> str:
    """A coarse device class from a user agent, for the sign-in's record."""
    ua = (user_agent or "").lower()
    if "ipad" in ua or "tablet" in ua:
        return "tablet"
    if "mobile" in ua or "iphone" in ua or "android" in ua:
        return "mobile"
    if any(os_ in ua for os_ in ("windows", "macintosh", "linux", "x11", "cros")):
        return "desktop"
    return "other"


def start(
    session: Session,
    sign_in_id: str,
    principal_id: str,
    expires_at: Optional[datetime],
    *,
    auth_method: str = "password",
    user_agent: Optional[str] = None,
    address: Optional[str] = None,
) -> SignIn:
    now = utcnow()
    row = SignIn(
        sign_in_id=sign_in_id,
        principal_id=principal_id,
        started_at=now,
        last_action_at=now,
        expires_at=expires_at,
        auth_method=auth_method,
        user_agent=user_agent,
        device=device_of(user_agent),
        address=address,
        refresh_generation=0,
    )
    session.add(row)
    return row


def make_room(session: Session, principal_id: str, limit: Optional[int]) -> int:
    """End the oldest open sign-ins so one more fits under ``limit``.

    Locks the principal first: two logins at once would otherwise each see room
    and both start. Call it in the transaction that calls ``start``.
    """
    if limit is None:
        return 0
    session.query(Principal.principal_id).filter(
        Principal.principal_id == principal_id
    ).with_for_update(key_share=True).first()
    open_rows = (
        session.query(SignIn)
        .filter(SignIn.principal_id == principal_id, SignIn.ended_at.is_(None))
        .order_by(SignIn.started_at)
        .with_for_update()
        .all()
    )
    now = utcnow()
    for row in open_rows[: max(0, len(open_rows) - (limit - 1))]:
        row.ended_at, row.end_reason = now, SIGN_IN_LIMIT
    return max(0, len(open_rows) - (limit - 1))


def end(sign_in_id: str, reason: str, session: Optional[Session] = None) -> bool:
    """End one open sign-in. True if this call ended it.

    For ``EXPIRY`` only once its latest refresh token has run out: an older,
    rotated token expires first and must not end a sign-in still in use.
    """
    with unit_of_work(session) as session:
        # Re-read under the lock: a row the caller already loaded may have ended since.
        row = session.get(
            SignIn, sign_in_id, with_for_update=True, populate_existing=True
        )
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


def stamp_action(sign_in_id: str, session: Session) -> None:
    """Record a person's own action now, unless the last stamp is under 30 s old.

    One conditional UPDATE: a burst of requests costs one write.
    """
    now = utcnow()
    session.query(SignIn).filter(
        SignIn.sign_in_id == sign_in_id,
        SignIn.ended_at.is_(None),
        or_(
            SignIn.last_action_at.is_(None),
            SignIn.last_action_at < now - STAMP_EVERY,
        ),
    ).update({SignIn.last_action_at: now}, synchronize_session=False)
