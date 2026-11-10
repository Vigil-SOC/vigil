"""Notices addressed to a person, and live warnings, for the console's bell.

A notice is a ``case_notifications`` row with no case: ``user_id`` is the
principal id of the person it is for. ``notify`` writes it and, when mail is
configured and the person has an active account, emails it too. Read state is
the row's ``is_read``/``read_at``. Rows that carry a case are the case
notifications' own and never listed here.

A condition is a live warning computed on each request and never stored:
register a function ``(user) -> [{"message", "link"}]`` in ``CONDITIONS``.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import func

from core.platform.email_service import mail_is_configured, send_email
from core.storage.connection import get_db_manager
from core.storage.models import CaseNotification, User
from core.time import utcnow

logger = logging.getLogger(__name__)

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
) -> int:
    """Writes a notice for ``principal_id``; returns its id. Never put a
    credential or token in ``message``: it is emailed and logged by the
    console mail backend in dev mode. Keep the ``link`` in the metadata."""
    meta = dict(metadata or {})
    if link:
        meta["link"] = link
    with get_db_manager().session_scope() as session:
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
        notice_id = row.notification_id
        if mail_is_configured():
            # Agents and the system have no user row, and a deactivated account gets no mail.
            to = (
                session.query(User.email)
                .filter(User.user_id == principal_id, User.is_active.is_(True))
                .scalar()
            )
            if to:
                send_email(to=to, subject=title, body=message)
                row.delivery_channel = "email"
                row.is_sent = True
                row.sent_at = utcnow()
    return notice_id


def _own_notices(session, user_id: str):
    return session.query(CaseNotification).filter(
        CaseNotification.user_id == user_id, CaseNotification.case_id.is_(None)
    )


def _shape(row: CaseNotification) -> Dict[str, Any]:
    return {
        "id": row.notification_id,
        "kind": row.notification_type,
        "title": row.title,
        "message": row.message,
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
            "notices": [_shape(r) for r in unread],
            "earlier": [_shape(r) for r in earlier],
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
