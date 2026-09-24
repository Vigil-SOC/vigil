"""Who a chat turn's tool calls act for, carried through the agent layer.

Chat does not call its tools from the request that authenticated the person: it
hands the turn to the agent layer, which calls back in over
``/internal/tools/invoke`` with a shared secret that says nothing about whom the
call is for. The API signs the person's name here, the agent layer carries it
opaquely, and the invoke endpoint verifies it before binding it with
``acting_as``. The agent layer can drop it; it cannot write one.

Two properties make this safe to hand to a process that is not the API:

- **It is not a session.** ``get_current_user`` accepts any JWT signed with
  ``JWT_SECRET_KEY`` that carries a ``user_id``. This is signed with a key
  derived from that secret for this purpose alone, so it fails that check, and
  it carries no ``user_id`` either way.
- **Nothing the agent layer holds can sign one.** ``AGENT_INTERNAL_TOKEN`` is
  not an input.

Stateless, so a replica other than the one that served the chat can verify it.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

from core.auth import auth_service

logger = logging.getLogger(__name__)

_PURPOSE = "vigil-tool-principal"
_ALGORITHM = "HS256"

# One chat turn: its wall budget (core/llm/chat_layers.py, 300s) plus the agent
# layer's LLM timeout (VIGIL_LLM_TIMEOUT_MS, 600s), since a last call started
# inside the budget may run that long before its tool calls go out -- with margin
# for limiter retries and the tool calls themselves.
TTL = timedelta(minutes=25)
# Clock skew between the replica that minted and the one that verifies.
_LEEWAY = timedelta(seconds=30)


class InvalidPrincipal(Exception):
    """A principal token that is forged, expired or not one of these."""


def _key() -> bytes:
    secret = auth_service.JWT_SECRET_KEY.encode()
    return hmac.new(secret, _PURPOSE.encode(), hashlib.sha256).digest()


def mint(username: str, ttl: timedelta = TTL) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": username, "aud": _PURPOSE, "iat": now, "exp": now + ttl},
        _key(),
        algorithm=_ALGORITHM,
    )


def verify(token: str) -> str:
    """The username the API signed, or ``InvalidPrincipal``. Never a fallback."""
    try:
        claims = jwt.decode(
            token,
            _key(),
            algorithms=[_ALGORITHM],
            audience=_PURPOSE,
            leeway=_LEEWAY,
            options={"require": ["sub", "aud", "exp"]},
        )
    except jwt.InvalidTokenError as exc:
        logger.warning("tool principal refused: %s", exc)
        raise InvalidPrincipal(str(exc)) from exc
    subject: Optional[str] = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise InvalidPrincipal("no subject")
    return subject
