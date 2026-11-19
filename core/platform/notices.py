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
import threading
from datetime import timedelta
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.audit import trail
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

# Subject slot of a bell notice about a person, addressed to someone else.
# Erasure deletes on these and nothing else. The title is the slot, except
# runs_stopped, whose title is shared. account_recovery titles are the
# person's own title with the leading "Your" replaced by "{username}'s",
# console and host script both. A reason, a run label, or the actor named
# only in the message is not the slot.
SUBJECT_TITLES: Dict[str, tuple] = {
    "mfa_change": ("{username} signed in with a recovery code",),
    "break_glass_used": ("Break-glass sign-in: {username}",),
    "sign_in_limit_repeat": ("{username}'s sign-ins were ended twice in an hour",),
    "password_leak": ("{username} reported a suspected password leak",),
    "account_recovery": (
        "{username}'s account was unlocked",
        "{username}'s password reset link",
        "{username}'s two-step sign-in was reset",
        "{username}'s account was reactivated",
        "{username}'s account was marked break-glass",
        "{username}'s account is no longer break-glass",
        "{username}'s account was restored",
    ),
    "dormancy_warning": ("{username} will be deactivated for inactivity",),
    "dormancy_held": ("{username} is idle and was kept: the last role manager",),
    "deactivated_work": ("{username} was deactivated and owns open work",),
    "account_locked": ("Account locked: {username}",),
}
# What runs_stopped's message opens with. The rest is run labels and counts.
RUNS_STOPPED_OPENING = "A sign-in of {username} ended ("
# retention.erased_username. Kept here so this module does not import retention:
# that import would pull the platform into every domain retention reaches.
_ERASED_PREFIX = "erased-"


def fill_subject(template: str, username: str) -> str:
    return template.replace("{username}", username, 1)


def names_subject(kind: str, username: str, title: str, message: str) -> bool:
    """True when this bell notice's subject slot is ``username``."""
    if kind == "runs_stopped":
        return message.startswith(fill_subject(RUNS_STOPPED_OPENING, username))
    templates = SUBJECT_TITLES.get(kind)
    if not templates:
        return False
    return title in {fill_subject(item, username) for item in templates}


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
    about_case: Optional[str] = None,
    about: Optional[str] = None,
) -> Optional[int]:
    """Writes a notice for ``principal_id`` and returns its id.

    With no ``session`` the row is committed, then emailed on this thread.
    With ``session`` the row is only added and flushed: nothing is mailed
    until ``send_notices`` runs after that transaction commits. A mail
    failure is recorded on the row and never raised. Never put a credential
    or token in ``message``: it is emailed and logged by the console mail
    backend in dev mode. Keep the ``link`` in the metadata. ``mail_body``
    is what the email says when it should say more than the stored
    ``message`` (which erasure can later need to blank). A notice
    ``about_case`` goes only to a reader of that case: anyone else gets nothing
    and None comes back (#4357). ``about`` is the principal whose username was
    read into this notice before the caller's commit. Once that account is the
    erased placeholder, the insert is skipped, unless the text already names
    the placeholder."""
    if session is not None:
        if not _reads(session, principal_id, about_case) or _erased_subject(
            session, about, kind, title, message
        ):
            return None
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
        if not _reads(owned, principal_id, about_case) or _erased_subject(
            owned, about, kind, title, message
        ):
            return None
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


def _erased_subject(
    session: Session,
    about: Optional[str],
    kind: str,
    title: str,
    message: str,
) -> bool:
    """The account ``about`` was erased after its username was read into this notice.

    Locks the user row erasure updates, in this insert's transaction. A notice
    that already names the erased placeholder was read after the rewrite.
    """
    if about is None:
        return False
    username = (
        session.query(User.username)
        .filter(User.principal_id == about)
        .with_for_update()
        .scalar()
    )
    if username != f"{_ERASED_PREFIX}{about}":
        return False
    return not names_subject(kind, username, title, message)


def _reads(session: Session, principal_id: str, case_id: Optional[str]) -> bool:
    if case_id is None:
        return True
    from core.storage import case_read_hook
    from core.storage.models import Case

    case = session.get(Case, case_id)
    return case is None or case_read_hook.may_read_as(case, principal_id)


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
    failed = None
    try:
        with get_db_manager().session_scope() as session:
            row = session.get(CaseNotification, notice_id)
            if row is None:
                return
            meta = dict(row.notification_metadata or {})
            if error:
                failed = (row.user_id, row.notification_type)
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
        return
    if failed:
        # The trail shows the person was not told: who, which kind, the error's
        # type. No address and no text, which erasure can blank on the notice.
        # On its own thread: the sender may be inside a request that holds the
        # trail's lock until it commits, and waiting here would deadlock on it.
        threading.Thread(
            target=_record_mail_failed, args=(*failed, error), daemon=True
        ).start()


def _record_mail_failed(principal: str, kind: str, error: str) -> None:
    """A notice's email that could not be sent: who, which kind, the error's type.

    Best effort and never held: a trail that is refusing must not make this
    fail again through the alert notice's own email.
    """
    try:
        with get_db_manager().session_scope() as session:
            if trail.in_use(session):
                trail.append(
                    session,
                    {
                        trail.ACTION: "notice.email_failed",
                        trail.ACTOR: trail.SYSTEM,
                        "principal": principal,
                        "kind": kind,
                        "error": error,
                    },
                )
    except Exception:  # noqa: BLE001
        logger.exception("audit trail did not record a failed notice email")


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
