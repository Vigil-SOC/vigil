"""Notices addressed to a person, and live warnings, for the console's bell.

A notice is a ``case_notifications`` row with no case: ``user_id`` is the
principal id of the person it is for. ``notify`` writes it and, when mail is
configured and the person has an active account, emails it after the row has
committed. Read state is the row's ``is_read``/``read_at``. Rows that carry a
case are the case notifications' own and never listed here.

A condition is a live warning computed on each request and never stored:
register a function ``(user) -> [{"message", "link"}]`` in ``CONDITIONS``.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.platform.email_service import mail_is_configured, send_email
from core.platform.user_agent import browser_of
from core.storage.connection import get_db_manager
from core.storage.models import CaseNotification, SignIn, User
from core.time import utcnow

logger = logging.getLogger(__name__)

# A send still inside the SMTP timeout (10s) must not read as unconfirmed.
# Past this, a pending row is one whose send never finished.
_UNCONFIRMED_AFTER = timedelta(seconds=60)

_MAIL_PENDING = "pending"
_MAIL_FAILED = "failed"
_LINE_FAILED = "email not sent"
_LINE_UNCONFIRMED = "email not confirmed"

# The mailed sentence when it says more than the stored message. It is not
# written on the row: erasure can blank ``message``, and the extra sentence
# (where a sign-in came from) must not become a second copy. The send runs
# on this process, after the commit, and reads it back from here.
_mail_bodies: Dict[int, str] = {}

# The notice to a person whose oldest sign-in a newer one ended under the sign-in limit.
SIGN_IN_LIMIT_KIND = "sign_in_limit"

# Rows returned: the unread ones (up to LISTED) and the newest EARLIER read ones.
LISTED = 50
EARLIER = 5

Condition = Callable[[User], List[Dict[str, str]]]
CONDITIONS: List[Condition] = []


def notify(
    principal_id: str,
    kind: str,
    title: str,
    message: str,
    link: Optional[str] = None,
    priority: str = "normal",
    metadata: Optional[Dict[str, Any]] = None,
    mail_body: Optional[str] = None,
    *,
    session: Optional[Session] = None,
) -> int:
    """Writes a notice for ``principal_id`` and returns its id.

    With no ``session`` the row is committed, then emailed on this thread.
    With ``session`` the row is only added and flushed: nothing is mailed
    until ``send_notices`` runs after that transaction commits. A mail
    failure is recorded on the row and never raised. Never put a credential
    or token in ``message``: it is emailed and logged by the console mail
    backend in dev mode. Keep the ``link`` in the metadata. ``mail_body``
    is what the email says when it should say more than the stored
    ``message`` (which erasure can later need to blank)."""
    if session is not None:
        return _insert(
            session,
            principal_id,
            kind,
            title,
            message,
            link,
            priority,
            metadata,
            mail_body,
        )
    with get_db_manager().session_scope() as owned:
        notice_id = _insert(
            owned,
            principal_id,
            kind,
            title,
            message,
            link,
            priority,
            metadata,
            mail_body,
        )
    send_notices([notice_id])
    return notice_id


def _active_email(session: Session, principal_id: str) -> Optional[str]:
    """The address mail goes to. Agents and the system have no user row, and
    a deactivated account gets no mail."""
    return (
        session.query(User.email)
        .filter(User.user_id == principal_id, User.is_active.is_(True))
        .scalar()
    )


def _insert(
    session: Session,
    principal_id: str,
    kind: str,
    title: str,
    message: str,
    link: Optional[str],
    priority: str,
    metadata: Optional[Dict[str, Any]],
    mail_body: Optional[str],
) -> int:
    meta = dict(metadata or {})
    if link:
        meta["link"] = link
    # Pending only when this notice is actually going to be emailed. Mail off,
    # or a principal with no address, leaves the metadata alone so the bell
    # gains no line.
    will_email = bool(mail_is_configured() and _active_email(session, principal_id))
    if will_email:
        meta["mail"] = _MAIL_PENDING
    row = CaseNotification(
        case_id=None,
        user_id=principal_id,
        notification_type=kind[:50],
        title=title[:200],
        message=message,
        priority=priority,
        notification_metadata=meta or None,
    )
    session.add(row)
    session.flush()
    if will_email and mail_body:
        _mail_bodies[row.notification_id] = mail_body
    return row.notification_id


def send_notices(notice_ids: List[int]) -> None:
    """Email each committed notice that is waiting on mail, and record the outcome.

    On this thread, after the caller's commit. A failure is logged and never
    raised, and does not roll back a save that already committed. An id that
    is not waiting is left alone."""
    for notice_id in notice_ids:
        try:
            _send_one(notice_id)
        except Exception:
            logger.exception("notice %s was not emailed", notice_id)


def _send_one(notice_id: int) -> None:
    extra = _mail_bodies.pop(notice_id, None)
    with get_db_manager().session_scope() as session:
        row = session.get(CaseNotification, notice_id)
        if (
            row is None
            or (row.notification_metadata or {}).get("mail") != _MAIL_PENDING
        ):
            return
        to = _active_email(session, row.user_id)
        subject = row.title
        body = extra or row.message
    if not to:
        # The address was there at insert and is gone now: nothing to send,
        # and no line, same as a notice that was never going to be emailed.
        _finish(notice_id, sent=False)
        return
    error = send_email(to=to, subject=subject, body=body)
    _finish(notice_id, sent=error is None, error=error)


def _finish(notice_id: int, *, sent: bool, error: Optional[str] = None) -> None:
    """Record the send. ``error`` is an exception class name; None with
    ``sent`` is success. Neither raises into the caller."""
    try:
        with get_db_manager().session_scope() as session:
            row = session.get(CaseNotification, notice_id)
            if row is None:
                return
            meta = dict(row.notification_metadata or {})
            if error:
                meta["mail"] = _MAIL_FAILED
                meta["mail_error"] = error
            else:
                meta.pop("mail", None)
                meta.pop("mail_error", None)
                if sent:
                    row.delivery_channel = "email"
                    row.is_sent = True
                    row.sent_at = utcnow()
            row.notification_metadata = meta or None
    except Exception:
        logger.exception("notice %s outcome was not recorded", notice_id)


def _own_notices(session, user_id: str):
    return session.query(CaseNotification).filter(
        CaseNotification.user_id == user_id, CaseNotification.case_id.is_(None)
    )


def _sign_in_limit_message(session, row: CaseNotification) -> str:
    """The sentence for a sign-in-limit notice, with where the new sign-in came
    from read off its row now, so erasure that blanked the row blanks it here."""
    sid = (row.notification_metadata or {}).get("sign_in_id")
    sign_in = session.get(SignIn, sid) if sid else None
    where = []
    if sign_in and sign_in.user_agent:
        where.append(
            browser_of(sign_in.user_agent)
            + (f" ({sign_in.device})" if sign_in.device else "")
        )
    if sign_in and sign_in.address:
        where.append(f"address {sign_in.address}")
    return row.message + (f" It came from {', '.join(where)}." if where else "")


def _mail_line(row: CaseNotification) -> str:
    """A fixed line for a notice that was going to be emailed and was not.
    Pending younger than ``_UNCONFIRMED_AFTER`` is a send still running."""
    state = (row.notification_metadata or {}).get("mail")
    if state == _MAIL_FAILED:
        return _LINE_FAILED
    if state == _MAIL_PENDING and row.created_at <= utcnow() - _UNCONFIRMED_AFTER:
        return _LINE_UNCONFIRMED
    return ""


def _shape(session, row: CaseNotification) -> Dict[str, Any]:
    message = (
        _sign_in_limit_message(session, row)
        if row.notification_type == SIGN_IN_LIMIT_KIND
        else row.message
    )
    line = _mail_line(row)
    if line:
        message = f"{message}\n{line}"
    return {
        "id": row.notification_id,
        "kind": row.notification_type,
        "title": row.title,
        "message": message,
        "link": (row.notification_metadata or {}).get("link"),
        "priority": row.priority,
        "read": row.is_read,
        "created_at": row.created_at.isoformat() + "Z",
    }


def conditions(user: User) -> List[Dict[str, str]]:
    """Every registered warning that holds for ``user`` now. One that fails is skipped."""
    out: List[Dict[str, str]] = []
    for check in CONDITIONS:
        try:
            out.extend(check(user))
        except Exception:
            logger.exception("Condition %s failed", getattr(check, "__name__", check))
    return out


def listing(user: User) -> Dict[str, Any]:
    """This person's notices, newest first: unread, then a few read ones, with the
    counts, and the live conditions."""
    with get_db_manager().session_scope() as session:
        base = _own_notices(session, user.user_id)
        unread_q = base.filter(CaseNotification.is_read.is_(False))
        unread = (
            unread_q.order_by(
                CaseNotification.created_at.desc(),
                CaseNotification.notification_id.desc(),
            )
            .limit(LISTED)
            .all()
        )
        earlier = (
            base.filter(CaseNotification.is_read.is_(True))
            .order_by(
                CaseNotification.created_at.desc(),
                CaseNotification.notification_id.desc(),
            )
            .limit(EARLIER)
            .all()
        )
        unread_total = unread_q.with_entities(func.count()).scalar()
        total = base.with_entities(func.count()).scalar()
        return {
            "notices": [_shape(session, r) for r in unread],
            "earlier": [_shape(session, r) for r in earlier],
            "unread": unread_total,
            "total": total,
            "conditions": conditions(user),
        }


def mark_read(user: User, ids: Optional[List[int]] = None) -> Dict[str, Any]:
    """Marks ``ids`` (or all) of this person's own notices read; returns the listing.
    An id that is not theirs is ignored."""
    with get_db_manager().session_scope() as session:
        q = _own_notices(session, user.user_id).filter(
            CaseNotification.is_read.is_(False)
        )
        if ids is not None:
            q = q.filter(CaseNotification.notification_id.in_(ids))
        q.update({"is_read": True, "read_at": utcnow()}, synchronize_session=False)
    return listing(user)
