"""Who a chat turn's tool calls act for, carried through the agent layer.

Chat does not call its tools from the request that authenticated the person: it
hands the turn to the agent layer, which calls back in over
``/internal/tools/invoke`` with a shared secret that says nothing about whom the
call is for. The API signs the person's name here, the agent layer carries it
opaquely, and the invoke endpoint verifies it before binding it with
``acting_as``. The agent layer can drop it; it cannot write one.

Two properties make this safe to hand to a process that is not the API:

- **It is not a session.** ``get_current_user`` accepts any JWT signed with
  the ring's sign-in key that carries a ``user_id``. This is signed with the
  ring's run-token key, so it fails that check, and it carries no ``user_id``
  either way.
- **Nothing the agent layer holds can sign one.** ``AGENT_INTERNAL_TOKEN`` is
  not an input.

Stateless, so a replica other than the one that served the chat can verify it.

A run token also carries the person principal (absent when no person started
it), the agent principal, the run id and the origin: person, schedule, alert
or parent, and the sign-in it started from (``sid``: none for schedule and
alert runs, a child carries its parent's). The username stays ``sub``. Nothing
here is a ``user_id``, and the key is not the sign-in one, so
``get_current_user`` refuses it.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

import jwt

from core.auth import key_ring

logger = logging.getLogger(__name__)

_PURPOSE = "vigil-tool-principal"
_ALGORITHM = "HS256"
ORIGINS = frozenset({"person", "schedule", "alert", "parent"})

# One chat turn: its wall budget (core/llm/chat_layers.py, 300s) plus the agent
# layer's LLM timeout (VIGIL_LLM_TIMEOUT_MS, 600s), since a last call started
# inside the budget may run that long before its tool calls go out -- with margin
# for limiter retries and the tool calls themselves.
TTL = timedelta(minutes=25)
# A run may sit parked for the longest park a caller may ask
# (core/agents/run_limits.py, budgets.max_park_ms, 30 days) and still call a
# tool when it is picked up again. Chat turns stay on TTL; they do not park.
RUN_TTL = timedelta(days=31)
# Clock skew between the replica that minted and the one that verifies.
_LEEWAY = timedelta(seconds=30)


class InvalidPrincipal(Exception):
    """A principal token that is forged, expired or not one of these."""


@dataclass(frozen=True)
class Claims:
    """What the API signed. ``username`` is absent when no person started it."""

    username: Optional[str]
    person_id: Optional[str]
    agent_id: Optional[str]
    run_id: Optional[str]
    origin: Optional[str]
    # The sign-in the run started from; absent for schedule and alert runs.
    sid: Optional[str] = None


_current: ContextVar[Optional[Claims]] = ContextVar("vigil_tool_claims", default=None)


@contextmanager
def bound_claims(claims: Optional[Claims]) -> Iterator[None]:
    """The signed principal for the tool call running on this task.

    Kept here rather than beside the trail writes, so a permission check can
    read it without importing what those writes import.
    """
    if claims is None:
        yield
        return
    token = _current.set(claims)
    try:
        yield
    finally:
        _current.reset(token)


def current_claims() -> Optional[Claims]:
    return _current.get()


def _claim(claims: dict, name: str) -> Optional[str]:
    value = claims.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise InvalidPrincipal(f"bad {name}")
    return value


def mint(
    username: Optional[str] = None,
    ttl: timedelta = TTL,
    *,
    person_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    run_id: Optional[str] = None,
    origin: Optional[str] = None,
    sid: Optional[str] = None,
) -> str:
    if origin is not None and origin not in ORIGINS:
        raise ValueError(f"origin must be one of {sorted(ORIGINS)}")
    now = datetime.now(timezone.utc)
    payload: dict = {"aud": _PURPOSE, "iat": now, "exp": now + ttl}
    if username:
        payload["sub"] = username
    if person_id:
        payload["person_id"] = person_id
    if agent_id:
        payload["agent_id"] = agent_id
    if run_id:
        payload["run_id"] = run_id
    if origin:
        payload["origin"] = origin
    if sid:
        payload["sid"] = sid
    return key_ring.sign("run", payload, _ALGORITHM)


def read(token: str) -> Claims:
    """The signed claims, or ``InvalidPrincipal``. Never a fallback."""
    try:
        claims = jwt.decode(
            token,
            key_ring.token_key("run", token),
            algorithms=[_ALGORITHM],
            audience=_PURPOSE,
            leeway=_LEEWAY,
            options={"require": ["aud", "exp"]},
        )
    except jwt.InvalidTokenError as exc:
        logger.warning("tool principal refused: %s", exc)
        raise InvalidPrincipal(str(exc)) from exc
    username = _claim(claims, "sub")
    agent_id = _claim(claims, "agent_id")
    if username is None and agent_id is None:
        raise InvalidPrincipal("no subject")
    origin = _claim(claims, "origin")
    if origin is not None and origin not in ORIGINS:
        raise InvalidPrincipal("bad origin")
    return Claims(
        username=username,
        person_id=_claim(claims, "person_id"),
        agent_id=agent_id,
        run_id=_claim(claims, "run_id"),
        origin=origin,
        sid=_claim(claims, "sid"),
    )


def sid_of(request) -> Optional[str]:
    """The sign-in the request's access token names (set by ``get_current_user``)."""
    claims = getattr(request.state, "access_claims", None) or {}
    return claims.get("sid") or None


def verify(token: str) -> str:
    """The username the API signed, or ``InvalidPrincipal``.

    A token no person started has no username; callers that need the agent
    and the origin use :func:`read`.
    """
    username = read(token).username
    if not username:
        raise InvalidPrincipal("no subject")
    return username
