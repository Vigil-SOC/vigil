"""Single-use links: an invite (#4226) by which a person sets up their own
account, and a password reset (#4227) for an existing one.

One ``auth_links`` row per link, kind ``invite`` or ``reset``. Only the SHA-256 of a random value is stored; the value is shown once
(the response, or the email) and never goes into a log, the trail or a notice.
A link is used up by the submission that completes it, not by opening it.
Nothing runs on a timer: an expired link is recorded on the trail the first time
something looks at it.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from core.audit.retention import ERASED_PREFIX
from core.audit.trail import SYSTEM
from core.auth import trail_events
from core.auth.password_reset import RESET_TTL_SECONDS
from core.storage.models import AuthLink
from core.time import utcnow

INVITE, RESET = "invite", "reset"
EMAIL, COPY = "email", "copy"
# A reset's channel says who asked for it. Only SELF, the self-service request,
# is held to the minimum password age; sign-in's forced change, an
# administrator's link and the host script's are not.
SELF, SIGNIN, HOST = "self", "signin", "host"
# Fixed when the link is issued: an emailed link is short, a copied one is handed over.
LIFETIME = {EMAIL: timedelta(hours=24), COPY: timedelta(days=7)}
# Refused passwords or taken usernames a link tolerates before it is dead.
MAX_ATTEMPTS = 10
# What every dead link says, whatever the reason.
INVALID = "This invite link isn't valid any more. Ask for a new invite."

USERNAME_RE = re.compile(r"[A-Za-z0-9._-]{3,64}")


def digest(token: str) -> str:
    return hashlib.sha256(token.strip().encode()).hexdigest()


def suggested_username(email: str) -> str:
    """The email's local part, trimmed to what a username may hold."""
    return re.sub(r"[^A-Za-z0-9._-]", "", email.split("@", 1)[0])[:64]


def username_problem(username: str) -> Optional[str]:
    if not USERNAME_RE.fullmatch(username):
        return "A username is 3 to 64 letters, digits, dots, dashes or underscores."
    if username.lower().startswith(("medic-", ERASED_PREFIX)):
        return "That username is reserved. Choose another."
    return None


def link_url(base: str, token: str) -> str:
    """The console URL that opens the accept screen for ``token``."""
    return f"{base.rstrip('/')}/accept-invite?token={token}"


def _open(session: Session, kind: str = INVITE):
    return session.query(AuthLink).filter(
        AuthLink.kind == kind,
        AuthLink.used_at.is_(None),
        AuthLink.revoked_at.is_(None),
    )


def pending(session: Session) -> list[AuthLink]:
    """Invites not yet used or revoked, expired ones included, newest first."""
    return _open(session).order_by(AuthLink.issued_at.desc()).all()


def find(
    session: Session, token: str, lock: bool = False, kind: str = INVITE
) -> Optional[AuthLink]:
    query = session.query(AuthLink).filter(
        AuthLink.kind == kind, AuthLink.token_hash == digest(token)
    )
    return (query.with_for_update() if lock else query).first()


def get(
    session: Session, link_id: str, lock: bool = False, kind: str = INVITE
) -> Optional[AuthLink]:
    query = _open(session, kind).filter(AuthLink.link_id == link_id)
    return (query.with_for_update() if lock else query).first()


def is_expired(row: AuthLink, now: Optional[datetime] = None) -> bool:
    return row.expires_at <= (now or utcnow())


def usable(row: Optional[AuthLink]) -> bool:
    return (
        row is not None
        and row.used_at is None
        and row.revoked_at is None
        and row.attempts < MAX_ATTEMPTS
        and not is_expired(row)
    )


def _about(row: AuthLink) -> tuple[Optional[dict], dict]:
    """The trail's beside-column and fields that name who a link is for."""
    if row.kind == RESET:
        return None, {"principal": row.principal_id}
    return {"personal": {"email": row.email}}, {}


def note_expired(session: Session, row: AuthLink) -> None:
    """Put an expired, unused link on the trail, once."""
    if row.used_at is not None or row.revoked_at is not None or not is_expired(row):
        return
    # The row's own update decides which of two concurrent lookers records it.
    marked = session.execute(
        update(AuthLink)
        .where(AuthLink.link_id == row.link_id, AuthLink.expired_noted_at.is_(None))
        .values(expired_noted_at=utcnow())
    )
    if marked.rowcount:
        beside, who = _about(row)
        trail_events.record(
            session,
            f"{row.kind}.expired",
            beside,
            actor=SYSTEM,
            link_id=row.link_id,
            role_id=row.role_id,
            **who,
        )


def revoke(
    session: Session,
    row: AuthLink,
    actor: str,
    reason: str = "revoked",
    record: bool = True,
) -> None:
    """Take an open link out of use: revoked by a person, replaced by a newer
    link, overtaken by single sign-on, or (a reset) by a change to the account.
    ``record=False`` leaves the trail to a caller whose own entry says why."""
    if record:
        note_expired(session, row)
    row.revoked_at = utcnow()
    session.flush()  # frees the address for the next invite
    if record:
        beside, who = _about(row)
        trail_events.record(
            session,
            f"{row.kind}.revoked",
            beside,
            actor=actor,
            link_id=row.link_id,
            reason=reason,
            **who,
        )
    row.email = None  # a closed link keeps no one's address


def open_for(session: Session, email: str, lock: bool = False) -> Optional[AuthLink]:
    """The email's open invite: at most one, by the table's unique index."""
    query = _open(session).filter(func.lower(AuthLink.email) == email.casefold())
    return (query.with_for_update() if lock else query).first()


def issue(
    session: Session, email: str, role_id: str, inviter: str, channel: str
) -> tuple[AuthLink, str]:
    """A new link for ``email``, replacing any open one. Returns the row and the
    value, which is not kept anywhere."""
    if old := open_for(session, email, lock=True):
        revoke(session, old, inviter, "replaced")
    token = secrets.token_urlsafe(32)  # 256 bits
    now = utcnow()
    row = AuthLink(
        link_id=f"link-{uuid.uuid4().hex[:12]}",
        kind=INVITE,
        token_hash=digest(token),
        email=email,
        role_id=role_id,
        invited_by=inviter,
        channel=channel,
        issued_at=now,
        expires_at=now + LIFETIME[channel],
    )
    session.add(row)
    session.flush()
    trail_events.record(
        session,
        "invite.sent",
        {"personal": {"email": email}},
        actor=inviter,
        link_id=row.link_id,
        role_id=role_id,
        channel=channel,
    )
    return row, token


def revoke_for_sso(session: Session, email: str) -> None:
    """The address signed in through single sign-on: its pending invite is moot."""
    if row := open_for(session, email, lock=True):
        revoke(session, row, SYSTEM, "sso")


# --- resets (#4227) ------------------------------------------------------------


def open_resets(session: Session, principal_id: str, lock: bool = False):
    """The account's reset links not yet used or revoked, newest first."""
    query = _open(session, RESET).filter(AuthLink.principal_id == principal_id)
    query = query.order_by(AuthLink.issued_at.desc())
    return (query.with_for_update() if lock else query).all()


def revoke_resets(
    session: Session,
    principal_id: str,
    actor: str,
    reason: str,
    record: bool = True,
    channels: Optional[tuple[str, ...]] = None,
) -> list[str]:
    """Close the account's open reset links (only ``channels``, if given):
    deactivated, deleted, its password or email changed, or a newer link
    issued. Returns their ids."""
    rows = open_resets(session, principal_id, lock=True)
    rows = [r for r in rows if channels is None or r.channel in channels]
    for row in rows:
        revoke(session, row, actor, reason, record)
    return [r.link_id for r in rows]


def issue_reset(
    session: Session, user, channel: str, actor: str, **trail: object
) -> tuple[AuthLink, str]:
    """A one-hour reset link for ``user``. Returns the row and the value, which
    is not kept anywhere. It replaces the open ones, except that the account's
    own request (or sign-in's) never cancels a link an administrator issued."""
    own = (SELF, SIGNIN)
    revoke_resets(
        session,
        user.principal_id,
        actor,
        "replaced",
        channels=own if channel in own else None,
    )
    token = secrets.token_urlsafe(32)  # 256 bits
    now = utcnow()
    row = AuthLink(
        link_id=f"link-{uuid.uuid4().hex[:12]}",
        kind=RESET,
        token_hash=digest(token),
        principal_id=user.principal_id,
        # Who issued it, for an administrator's link; else the account asked.
        invited_by=actor if channel in (EMAIL, COPY) else None,
        channel=channel,
        issued_at=now,
        expires_at=now + timedelta(seconds=RESET_TTL_SECONDS),
    )
    session.add(row)
    session.flush()
    trail_events.record(
        session,
        "reset.sent",
        actor=actor,
        principal=user.principal_id,
        link_id=row.link_id,
        channel=channel,
        **trail,
    )
    return row, token


def forced(row: AuthLink) -> bool:
    """Whether the minimum password age is waived: anything but a self-service request."""
    return row.channel != SELF
