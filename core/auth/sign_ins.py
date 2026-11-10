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

from pydantic import BaseModel
from sqlalchemy import event, or_
from sqlalchemy.orm import Session

from core.storage.models import Principal, SignIn, User
from core.storage.schemas.base import IsoDateTime, OptDateTime
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
# Ended from the sign-in list or the person's detail (#4172).
ENDED_BY_PERSON = "ended_by_person"
ENDED_BY_ADMIN = "ended_by_admin"  # also a service account or playbook, as containment
EVERYONE = "everyone_signed_out"
# A newer sign-in took this one's place under the account's sign-in limit.
SIGN_IN_LIMIT = "sign_in_limit"

# The end reasons that stop the sign-in's agent runs (and their children): a
# compromise, someone else's act, or a change of who the account is. Time, idle,
# eviction, sign-out and upgrade endings stop nothing, and a reason not listed
# here stops nothing. A sign-in a person acts from keeps its runs unless it is
# itself ended.
STOPS_RUNS = frozenset(
    {
        TOKEN_REUSE,
        ACCOUNT_CHANGED,
        DEACTIVATED,
        ERASED,
        ENDED_BY_PERSON,
        ENDED_BY_ADMIN,
        EVERYONE,
    }
)

# What a stopped run reads, and what its tool calls are refused with.
STOP_REASON = "stopped: sign-in ended"

# How each end reason reads in the console (epic #4164 §8).
REASON_WORDS = {
    SIGN_OUT: "Signed out",
    EXPIRY: "Ran out",
    IDLE: "Idle too long",
    LENGTH_LIMIT: "Reached the sign-in length limit",
    TOKEN_REUSE: "Token reuse",
    UPGRADE: "Ended by an upgrade",
    ACCOUNT_CHANGED: "Role, password, email or methods changed",
    DEACTIVATED: "Account deactivated or deleted",
    ERASED: "The person's details were erased",
    ENDED_BY_PERSON: "Ended by the person",
    ENDED_BY_ADMIN: "Ended by an admin",
    EVERYONE: "Everyone signed out",
    SIGN_IN_LIMIT: "A newer sign-in took its place",
}
UNNAMED_REASON = "Ended by a security event"

# Ended rows stay in a list this long, and a list holds at most this many.
LISTED_FOR = timedelta(days=30)
LISTED = 50

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


class SignInEntry(BaseModel):
    """One sign-in in a list."""

    sign_in_id: str
    started_at: IsoDateTime
    last_action_at: IsoDateTime
    browser: str
    device: Optional[str] = None
    address: Optional[str] = None
    # 'password' or 'mfa'
    auth_method: str
    ended_at: OptDateTime = None
    end_reason: Optional[str] = None
    end_reason_text: Optional[str] = None
    # The sign-in the list was asked from (a person's own list only).
    current: bool = False


class EndedSignIns(BaseModel):
    ended: int
    # The caller's own sign-in was among them: its cookies are cleared.
    signed_out: bool = False


def browser_of(user_agent: Optional[str]) -> str:
    """A coarse "Browser on OS" from a user agent, for a list. No dependency."""
    ua = user_agent or ""
    browser = next(
        (
            name
            for token, name in (
                ("Edg/", "Edge"),
                ("OPR/", "Opera"),
                ("Firefox/", "Firefox"),
                ("FxiOS", "Firefox"),
                ("CriOS", "Chrome"),
                ("Chrome/", "Chrome"),
                ("Safari/", "Safari"),
            )
            if token in ua
        ),
        None,
    )
    os_ = next(
        (
            name
            for token, name in (
                ("iPhone", "iOS"),
                ("iPad", "iOS"),
                ("Android", "Android"),
                ("Windows", "Windows"),
                ("Macintosh", "macOS"),
                ("CrOS", "ChromeOS"),
                ("Linux", "Linux"),
                ("X11", "Linux"),
            )
            if token in ua
        ),
        None,
    )
    if browser and os_:
        return f"{browser} on {os_}"
    return browser or os_ or "Unknown browser"


def shape(row: SignIn, current: Optional[str] = None) -> dict:
    """One sign-in as a list shows it. Times are naive UTC."""
    return {
        "sign_in_id": row.sign_in_id,
        "started_at": row.started_at,
        "last_action_at": row.last_action_at,
        "browser": browser_of(row.user_agent),
        "device": row.device,
        "address": row.address,
        "auth_method": row.auth_method,
        "ended_at": row.ended_at,
        "end_reason": row.end_reason,
        "end_reason_text": (
            REASON_WORDS.get(row.end_reason, UNNAMED_REASON) if row.ended_at else None
        ),
        "current": row.sign_in_id == current,
    }


def listing(
    session: Session, principal_id: str, current: Optional[str] = None
) -> list[dict]:
    """A principal's open sign-ins and recently ended ones, newest first."""
    now = utcnow()
    live = SignIn.ended_at.is_(None) & or_(
        # An open row whose refresh token ran out is over, just not yet marked.
        SignIn.expires_at.is_(None),
        SignIn.expires_at > now,
    )
    rows = (
        session.query(SignIn)
        .filter(
            SignIn.principal_id == principal_id,
            or_(live, SignIn.ended_at > now - LISTED_FOR),
        )
        .order_by(SignIn.started_at.desc())
        .limit(LISTED)
        .all()
    )
    return [shape(row, current) for row in rows]


def open_ids(
    session: Session, principal_id: Optional[str] = None, keep: Optional[str] = None
) -> list[tuple[str, str]]:
    """(sign-in id, principal id) of open sign-ins, one principal's or all, to end
    one by one through ``end``. ``keep`` is left out."""
    query = session.query(SignIn.sign_in_id, SignIn.principal_id).filter(
        SignIn.ended_at.is_(None)
    )
    if principal_id is not None:
        query = query.filter(SignIn.principal_id == principal_id)
    if keep is not None:
        query = query.filter(SignIn.sign_in_id != keep)
    return [(sid, pid) for sid, pid in query.all()]


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


# Sign-ins ended for a stopping reason wait on the session until it commits: the
# callers end rows inside their own transaction, and a rolled-back change must not
# stop runs.
_PENDING = "sign_in_stops"


def _queue_stop(session: Session, sign_in_id: str, principal_id: str, reason: str):
    session.info.setdefault(_PENDING, []).append((sign_in_id, principal_id, reason))


@event.listens_for(Session, "after_commit")
def _stop_runs(session: Session) -> None:
    pending = session.info.pop(_PENDING, None)
    if pending:
        from core.auth import sign_in_runs

        sign_in_runs.stop(pending)


@event.listens_for(Session, "after_rollback")
def _drop_stops(session: Session) -> None:
    session.info.pop(_PENDING, None)


def ended_for_compromise(sign_in_id: str) -> bool:
    """True when the sign-in ended for a reason that stops its runs."""
    with unit_of_work() as session:
        reason = (
            session.query(SignIn.end_reason)
            .filter(SignIn.sign_in_id == sign_in_id, SignIn.ended_at.isnot(None))
            .scalar()
        )
    return reason in STOPS_RUNS


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
        if reason in STOPS_RUNS:
            _queue_stop(session, sign_in_id, row.principal_id, reason)
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
        open_rows = SignIn.principal_id == principal_id, SignIn.ended_at.is_(None)
        if reason in STOPS_RUNS:
            for (sid,) in session.query(SignIn.sign_in_id).filter(*open_rows):
                _queue_stop(session, sid, principal_id, reason)
        return (
            session.query(SignIn)
            .filter(*open_rows)
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
