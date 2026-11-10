"""Invites (#4226): a single-use link through which a person sets their own
password and becomes an account.

Only the SHA-256 of a link's value is stored (``account_links``). The value
itself appears once, in the response that issued a copied link or in the
invite email, and never in a log, the trail or a notice. Opening a link uses
nothing up; the submission that creates the account does.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from core.audit.trail import SYSTEM
from core.auth import principals, trail_events
from core.auth.permissions import KNOWN_PERMISSIONS
from core.config import get_settings
from core.platform.email_service import mail_is_configured
from core.storage.models import AccountLink, Principal, Role, User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)

INVITE = "invite"
EMAIL, COPY = "email", "copy"
# Fixed when the link is issued.
LIFETIME = {EMAIL: timedelta(hours=24), COPY: timedelta(days=7)}
# Rejected submissions (a refused password or a taken username) before the
# link stops working.
MAX_ATTEMPTS = 10
PATH = "/accept-invite"
# One reply for an unknown, expired, revoked, used or exhausted link.
NOT_USABLE = "This invite link can't be used any more. Ask for a new invite."


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def link_path(token: str) -> str:
    # In the fragment, so loading the page never sends the value to a server.
    return f"{PATH}#token={token}"


def link_url(token: str) -> Optional[str]:
    """The full link from VIGIL_FRONTEND_URL, or None when it isn't set. Never
    from the request's Host header, which the client controls."""
    base = get_settings().vigil_frontend_url.strip()
    return f"{base.rstrip('/')}{link_path(token)}" if base else None


def email_unavailable() -> Optional[str]:
    """Why invites can't be emailed here, or None when they can."""
    missing = []
    if not mail_is_configured():
        missing.append("VIGIL_EMAIL_BACKEND=smtp with its SMTP settings")
    if not get_settings().vigil_frontend_url.strip():
        missing.append("VIGIL_FRONTEND_URL")
    if not missing:
        return None
    return f"To email invites, set {' and '.join(missing)} on the server."


def issue(
    session: Session, email: str, role_id: str, inviter: str, channel: str
) -> tuple[AccountLink, str]:
    """A new invite row and its raw value. The caller has cleared the email."""
    token = secrets.token_urlsafe(32)
    now = utcnow()
    link = AccountLink(
        link_id=f"invite-{uuid.uuid4().hex[:12]}",
        kind=INVITE,
        token_hash=digest(token),
        email=email,
        role_id=role_id,
        invited_by=inviter,
        channel=channel,
        issued_at=now,
        expires_at=now + LIFETIME[channel],
    )
    session.add(link)
    session.flush()
    trail_events.record(
        session,
        "invite.sent",
        {"personal": {"email": email}},
        actor=inviter,
        link_id=link.link_id,
        role_id=role_id,
        channel=channel,
    )
    return link, token


def find(session: Session, token: str, lock: bool = False) -> Optional[AccountLink]:
    query = session.query(AccountLink).filter(
        AccountLink.kind == INVITE, AccountLink.token_hash == digest(token or "")
    )
    return (query.with_for_update() if lock else query).first()


def expired(link: AccountLink, now: Optional[datetime] = None) -> bool:
    """Not used or revoked, and past its expiry."""
    return (
        link.used_at is None
        and link.revoked_at is None
        and link.expires_at <= (now or utcnow())
    )


def usable(link: Optional[AccountLink]) -> bool:
    return (
        link is not None
        and link.used_at is None
        and link.revoked_at is None
        and link.expires_at > utcnow()
        and link.attempts < MAX_ATTEMPTS
    )


def record_expiry(session: Session, link: AccountLink) -> None:
    """``invite.expired``, once per link. There is no scheduler: whoever first
    sees the link expired (its holder, the list, a re-invite) writes it."""
    if link.expiry_recorded_at is not None or not expired(link):
        return
    # Conditional, so of two requests that both saw it unrecorded one writes.
    now = utcnow()
    won = (
        session.query(AccountLink)
        .filter(
            AccountLink.link_id == link.link_id,
            AccountLink.expiry_recorded_at.is_(None),
        )
        .update({AccountLink.expiry_recorded_at: now}, synchronize_session=False)
    )
    if won:
        link.expiry_recorded_at = now
        trail_events.record(
            session,
            "invite.expired",
            {"personal": {"email": link.email}},
            actor=SYSTEM,
            link_id=link.link_id,
        )


def record_expiry_alone(link_id: str) -> None:
    """record_expiry in its own transaction, for a request that refuses."""
    try:
        with unit_of_work() as session:
            link = session.get(AccountLink, link_id, with_for_update=True)
            if link is not None:
                record_expiry(session, link)
    except Exception:  # noqa: BLE001 - the link is refused regardless
        logger.exception("could not record invite %s expiring", link_id)


def revoke(
    session: Session, link: AccountLink, actor: str, reason: str = "revoked"
) -> None:
    """Close a pending invite. An expired one is closed quietly, its expiry
    recorded instead: it was no longer usable."""
    if expired(link):
        record_expiry(session, link)
    else:
        trail_events.record(
            session,
            "invite.revoked",
            {"personal": {"email": link.email}},
            actor=actor,
            link_id=link.link_id,
            reason=reason,
        )
    link.revoked_at = utcnow()
    session.flush()


def inviter_problem(session: Session, link: AccountLink) -> Optional[str]:
    """Why the invite no longer stands, or None. The inviter must still be an
    active person who may invite and whose permissions cover the role."""
    role = session.get(Role, link.role_id) if link.role_id else None
    if role is None:
        return "The role this invite gives no longer exists."
    principal = session.get(Principal, link.invited_by) if link.invited_by else None
    inviter = (
        session.query(User).filter(User.principal_id == link.invited_by).first()
        if principal is not None
        else None
    )
    if (
        inviter is None
        or principal.kind != principals.PERSON
        or principal.deleted_at is not None
        or not inviter.is_active
        or inviter.service_account is True
    ):
        return "The person who invited you can no longer give access."
    inviter_role = session.get(Role, inviter.role_id)
    held = (inviter_role.permissions if inviter_role else None) or {}
    granted = {k for k, on in (role.permissions or {}).items() if on}
    if not held.get("users.write") or any(
        not held.get(k) for k in granted & KNOWN_PERMISSIONS
    ):
        return "The person who invited you can no longer give this access."
    return None


def suggested_username(email: str) -> str:
    return email.split("@", 1)[0]
