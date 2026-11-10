"""Minting, checking, rotating and revoking the API tokens programs hold.

A session says a person signed in a moment ago. A credential here says a
program was given standing access by an operator who is not present. They are
different claims, so they are different things, and neither is accepted where
the other belongs: ``authenticate`` refuses anything that is not one of these,
and the session layer never sees one because it does not carry a session's
shape.

The token exists once, in the return value of :func:`mint`. What is stored is a
SHA-256 of it. Nothing can recover it, so an operator who loses one mints
another and revokes the first -- which is the behaviour we want anyway, and the
reason revocation is cheap.

Two prefixes share the table. ``vgl_api_`` is what is minted now and reaches the
HTTP API as well as MCP; ``vgl_mcp_`` is an earlier token and stays MCP-only.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta
from functools import partial
from typing import List, NamedTuple, Optional

from sqlalchemy.orm import Session

from core.auth import trail_events
from core.storage.models import McpCredential, User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)

# What a token looks like, so that a credential presented in the wrong place is
# refused with a reason rather than a shrug, and so one pasted into a log or an
# issue is recognisable as a secret by anyone reading it. Whether a token may
# reach the HTTP API is a property of its prefix.
TOKEN_PREFIX = "vgl_api_"
MCP_TOKEN_PREFIX = "vgl_mcp_"

# How long a token lives, by who holds it. There is no "never".
PERSON_DEFAULT_DAYS = 90
PERSON_MAX_DAYS = 365
SERVICE_ACCOUNT_DAYS = 365
# What a rotated token keeps working for, unless it is suspected compromised.
ROTATION_GRACE = timedelta(hours=24)

# 32 bytes from the OS CSPRNG. url-safe so it survives a header, a shell, and
# a JSON config file without escaping.
_TOKEN_BYTES = 32


class MintedCredential(NamedTuple):
    """The one moment the token exists. ``record`` is what persists."""

    token: str
    record: McpCredential


class AuthenticatedCredential(NamedTuple):
    """The user a credential stands for, and which credential it was.

    ``authenticate`` used to return only the user, and the credential id was
    gone before the MCP surface could put it on the close.
    """

    user: User
    credential_id: str


def looks_like_mcp_token(value: str) -> bool:
    """Whether ``value`` is presented as an MCP-only token.

    Used to refuse it somewhere it does not belong, which is a different
    answer from "this is not valid" and should read differently to whoever
    sent it.
    """
    return bool(value) and value.startswith(MCP_TOKEN_PREFIX)


def looks_like_api_token(value: str) -> bool:
    """Whether ``value`` is presented as a token that may reach the HTTP API."""
    return bool(value) and value.startswith(TOKEN_PREFIX)


def looks_like_token(value: str) -> bool:
    return looks_like_api_token(value) or looks_like_mcp_token(value)


class NotRotatable(Exception):
    """The token is rotated, revoked or expired already."""


def lifetime(user: User, days: Optional[int] = None) -> timedelta:
    """How long a token for ``user`` lives; ValueError for a request out of range.

    One rule for every minter: a person's defaults to 90 days and is 365 at most;
    a service account's is 365.
    """
    if user.service_account is True:
        return timedelta(days=SERVICE_ACCOUNT_DAYS)
    days = PERSON_DEFAULT_DAYS if days is None else days
    if not 1 <= days <= PERSON_MAX_DAYS:
        raise ValueError(f"A token lasts 1 to {PERSON_MAX_DAYS} days")
    return timedelta(days=days)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint(
    user_id: str,
    label: str,
    days: Optional[int] = None,
    session: Optional[Session] = None,
    actor: Optional[str] = None,
) -> Optional[MintedCredential]:
    """Issue a token for ``user_id``. Returns None if there is no such user.

    It expires after :func:`lifetime` of ``days``. The caller must show the token
    to the operator now; it is not recoverable. ``actor`` is the principal minting
    it; its trail entry commits with it.
    """
    with unit_of_work(session) as session:
        user = session.query(User).filter(User.user_id == user_id).first()
        if user is None:
            logger.warning("Refusing to mint a token for unknown user")
            return None

        expires_at = utcnow() + lifetime(user, days)
        token = TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)
        record = McpCredential(
            credential_id=f"mcpc-{uuid.uuid4().hex[:16]}",
            user_id=user_id,
            token_hash=_hash(token),
            label=label,
            expires_at=expires_at,
        )
        session.add(record)
        session.flush()
        trail_events.record(
            session,
            "credential.minted",
            actor=actor,
            principal=user.principal_id,
            credential_id=record.credential_id,
            expires_at=expires_at.isoformat(),
        )

        logger.info(
            "Minted token %s for user %s (%s)",
            record.credential_id,
            user_id,
            label,
        )
        return MintedCredential(token=token, record=record)


def authenticate(
    token: str, session: Optional[Session] = None
) -> Optional[AuthenticatedCredential]:
    """The user a token stands for, and the credential id, or None.

    None covers every way this can fail -- not one of ours, unknown, revoked,
    expired, or belonging to a deactivated account -- because a caller holding
    a credential that does not work learns nothing useful from which, and an
    attacker probing would learn plenty. The user's role is not read here:
    a credential carries none of its own, and each tool checks the live role.
    """
    if not looks_like_token(token):
        return None

    refusal = None
    try:
        with unit_of_work(session) as session:
            record = (
                session.query(McpCredential)
                .filter(McpCredential.token_hash == _hash(token))
                .first()
            )
            if record is None or record.revoked_at is not None:
                return None
            if not record.is_usable():
                refusal = partial(trail_events.credential_expired, record.credential_id)
                return None

            user = session.query(User).filter(User.user_id == record.user_id).first()
            if user is None or not user.is_active:
                if user is not None:
                    refusal = partial(
                        trail_events.credential_inactive,
                        record.credential_id,
                        user.principal_id,
                    )
                return None

            # Written on the way through so an unused credential can be found
            # later. The Ledger records what was done; this only records that
            # something was.
            record.last_used_at = utcnow()
            return AuthenticatedCredential(user, record.credential_id)
    except Exception:  # noqa: BLE001 - a database that cannot answer is not a yes
        # A store this cannot read has not authenticated anybody. Saying so is
        # a refusal, not an error: the caller is told it is not authenticated,
        # which is true, rather than being handed a 500 that says the surface
        # is broken and invites a retry.
        logger.exception("Could not check a token; refusing the caller")
        return None
    finally:
        # After the lookup's transaction, on a session of its own.
        if refusal is not None:
            refusal()


def revoke(credential_id: str, session: Optional[Session] = None) -> bool:
    """Withdraw a credential. True if this call is what withdrew it."""
    with unit_of_work(session) as session:
        record = (
            session.query(McpCredential)
            .filter(McpCredential.credential_id == credential_id)
            .with_for_update()
            .first()
        )
        if record is None or record.revoked_at is not None:
            return False

        record.revoked_at = utcnow()
        logger.info("Revoked token %s", credential_id)
        return True


def list_for_user(
    user_id: str,
    include_revoked: bool = False,
    session: Optional[Session] = None,
) -> List[McpCredential]:
    """Credentials belonging to one person, newest first."""
    with unit_of_work(session) as session:
        query = session.query(McpCredential).filter(McpCredential.user_id == user_id)
        if not include_revoked:
            query = query.filter(McpCredential.revoked_at.is_(None))
        return query.order_by(McpCredential.created_at.desc()).all()


def get(credential_id: str, session: Optional[Session] = None) -> Optional[McpCredential]:
    with unit_of_work(session) as session:
        return session.get(McpCredential, credential_id)


def list_all(
    holder: Optional[str] = None, session: Optional[Session] = None
) -> List[tuple[McpCredential, User]]:
    """Every token with its holder, newest first, for one holder when given."""
    with unit_of_work(session) as session:
        query = session.query(McpCredential, User).join(
            User, User.user_id == McpCredential.user_id
        )
        if holder:
            query = query.filter(McpCredential.user_id == holder)
        return query.order_by(McpCredential.created_at.desc()).all()


def holders(session: Optional[Session] = None) -> List[User]:
    """Everyone who holds a token, for a filter."""
    with unit_of_work(session) as session:
        return (
            session.query(User)
            .filter(
                User.user_id.in_(session.query(McpCredential.user_id).distinct())
            )
            .order_by(User.username)
            .all()
        )


def rotate(
    credential_id: str,
    compromised: bool = False,
    days: Optional[int] = None,
    session: Optional[Session] = None,
    actor: Optional[str] = None,
) -> Optional[tuple[MintedCredential, McpCredential]]:
    """Mint a successor with the same holder and label; returns it with the old
    record as it now stands, or None if there is no such token.

    The old token then ends at the earlier of its own expiry and 24 hours from
    now, or now when ``compromised``. NotRotatable once it is rotated, revoked or
    expired. The row is locked, so two rotations of one token cannot both win.
    """
    with unit_of_work(session) as session:
        old = (
            session.query(McpCredential)
            .filter(McpCredential.credential_id == credential_id)
            .with_for_update()
            .first()
        )
        if old is None:
            return None
        now = utcnow()
        if old.status(now) != "active":
            raise NotRotatable(f"This token is {old.status(now)}")
        minted = mint(old.user_id, old.label, days, session=session, actor=actor)
        if minted is None:
            return None
        end = now if compromised else now + ROTATION_GRACE
        old.expires_at = min(old.expires_at, end) if old.expires_at else end
        old.successor_id = minted.record.credential_id
        holder = session.get(User, old.user_id)
        trail_events.record(
            session,
            "credential.rotated",
            actor=actor,
            principal=holder.principal_id,
            credential_id=credential_id,
            successor_id=old.successor_id,
            compromised=True if compromised else None,
        )
        return minted, old
