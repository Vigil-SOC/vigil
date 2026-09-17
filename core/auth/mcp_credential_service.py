"""Minting, checking and revoking the credentials programs use on MCP.

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
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import datetime
from typing import List, NamedTuple, Optional

from sqlalchemy.orm import Session

from core.storage.models import McpCredential, User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)

# What a token looks like, so that a credential presented in the wrong place is
# refused with a reason rather than a shrug, and so one pasted into a log or an
# issue is recognisable as a secret by anyone reading it.
TOKEN_PREFIX = "vgl_mcp_"

# 32 bytes from the OS CSPRNG. url-safe so it survives a header, a shell, and
# a JSON config file without escaping.
_TOKEN_BYTES = 32


class MintedCredential(NamedTuple):
    """The one moment the token exists. ``record`` is what persists."""

    token: str
    record: McpCredential


def looks_like_mcp_token(value: str) -> bool:
    """Whether ``value`` is presented as one of these credentials.

    Used to refuse it somewhere it does not belong, which is a different
    answer from "this is not valid" and should read differently to whoever
    sent it.
    """
    return bool(value) and value.startswith(TOKEN_PREFIX)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint(
    user_id: str,
    label: str,
    expires_at: Optional[datetime] = None,
    session: Optional[Session] = None,
) -> Optional[MintedCredential]:
    """Issue a credential for ``user_id``. Returns None if there is no such user.

    The caller must show the token to the operator now; it is not recoverable.
    """
    with unit_of_work(session) as session:
        user = session.query(User).filter(User.user_id == user_id).first()
        if user is None:
            logger.warning("Refusing to mint an MCP credential for unknown user")
            return None

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

        logger.info(
            "Minted MCP credential %s for user %s (%s)",
            record.credential_id,
            user_id,
            label,
        )
        return MintedCredential(token=token, record=record)


def authenticate(token: str, session: Optional[Session] = None) -> Optional[User]:
    """The user a token stands for, or None.

    None covers every way this can fail -- not one of ours, unknown, revoked,
    expired, or belonging to a deactivated account -- because a caller holding
    a credential that does not work learns nothing useful from which, and an
    attacker probing would learn plenty.
    """
    if not looks_like_mcp_token(token):
        return None

    with unit_of_work(session) as session:
        record = (
            session.query(McpCredential)
            .filter(McpCredential.token_hash == _hash(token))
            .first()
        )
        if record is None or not record.is_usable():
            return None

        user = session.query(User).filter(User.user_id == record.user_id).first()
        if user is None or not user.is_active:
            return None

        # Written on the way through so an unused credential can be found
        # later. The Ledger records what was done; this only records that
        # something was.
        record.last_used_at = utcnow()
        return user


def revoke(credential_id: str, session: Optional[Session] = None) -> bool:
    """Withdraw a credential. True if this call is what withdrew it."""
    with unit_of_work(session) as session:
        record = (
            session.query(McpCredential)
            .filter(McpCredential.credential_id == credential_id)
            .first()
        )
        if record is None or record.revoked_at is not None:
            return False

        record.revoked_at = utcnow()
        logger.info("Revoked MCP credential %s", credential_id)
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
