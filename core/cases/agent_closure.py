"""Who an agent tool call acts as, and the session it writes in."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.audit import trail
from core.cases.closure import CHANNEL_AGENT, CHANNEL_COOKIE
from core.integrations.mcp.surface import current_caller
from core.storage.connection import get_db_session

# No principal bound: nobody is behind the call, as in a hunt.
CALLER_UNAUTHENTICATED = "agent"

# One trail entry per close. The username sits beside the hash.
CLOSE_ACTION = "case.close"

_http: ContextVar[Optional["HttpActor"]] = ContextVar("vigil_http_actor", default=None)
_credential: ContextVar[Optional[str]] = ContextVar(
    "vigil_close_credential", default=None
)


@dataclass(frozen=True)
class HttpActor:
    """The person ``get_current_user`` just accepted, and which door it was."""

    username: str
    principal_id: Optional[str]
    # ``cookie`` or ``bearer``. Dev mode notes nothing: the bypass is not a cookie.
    via: str
    person: bool


def note_http_actor(
    *,
    username: str,
    principal_id: Optional[str],
    via: str,
    person: bool,
) -> HttpActor:
    """Remember the signed-in user for the rest of this request."""
    actor = HttpActor(
        username=username,
        principal_id=principal_id if isinstance(principal_id, str) else None,
        via=via,
        person=person,
    )
    _http.set(actor)
    return actor


def clear_http_actor() -> None:
    """A request that did not accept a session has no HTTP actor."""
    _http.set(None)


@contextmanager
def bound_credential(credential_id: Optional[str]) -> Iterator[None]:
    """The MCP credential this call authenticated with, beside the username."""
    token = _credential.set(credential_id)
    try:
        yield
    finally:
        _credential.reset(token)


def current_credential_id() -> Optional[str]:
    return _credential.get()


def close_channel() -> str:
    """``cookie`` only when the access cookie authenticated a person.

    A bearer token, a credential, a verified tool principal, an unset caller,
    and the dev-mode bypass are ``agent``. The cookie wins when a bearer is
    also present, because that is the door ``get_current_user`` already took.
    """
    actor = _http.get()
    if actor is not None and actor.via == "cookie" and actor.person:
        return CHANNEL_COOKIE
    return CHANNEL_AGENT


def principal_for_username(session: Session, username: str) -> Optional[str]:
    """The principal id of a live username, or None.

    Deleted principals are not live. A run id, ``system``, or any other
    non-username is left as text — this does not mint ``agent:<key>``.
    """
    if not username:
        return None
    from core.storage.models import Principal, User

    row = (
        session.query(User.principal_id)
        .join(Principal, Principal.principal_id == User.principal_id)
        .filter(User.username == username, Principal.deleted_at.is_(None))
        .one_or_none()
    )
    return None if row is None else row[0]


def actor_identity(
    session: Optional[Session] = None, explicit: Optional[str] = None
) -> tuple[Optional[str], str]:
    """``(principal id or None, actor text)`` for this call.

    ``explicit`` is text the caller already had (a merge's ``merged_by``, a
    run id). Otherwise the text is the bound caller, the signed-in username,
    or ``agent`` when nobody is bound. An unset caller stores no principal.
    """
    http = _http.get()
    caller = current_caller()
    if explicit is None:
        if caller:
            name = caller
        elif http is not None and http.username:
            name = http.username
        else:
            return None, CALLER_UNAUTHENTICATED
    else:
        name = explicit or CALLER_UNAUTHENTICATED
    # The sentinel is not a username, even if an account is named "agent".
    if name == CALLER_UNAUTHENTICATED and caller is None and http is None:
        return None, CALLER_UNAUTHENTICATED
    if session is None:
        from core.storage.unit_of_work import unit_of_work

        with unit_of_work() as owned:
            principal = principal_for_username(owned, name)
    else:
        principal = principal_for_username(session, name)
    return principal, name


def activity_actor(
    explicit: Optional[str] = None, session: Optional[Session] = None
) -> dict[str, str]:
    """Principal id and the text kept beside it, for one new activity entry.

    Old activity JSON is not rewritten. The text already on the entry stays;
    this only adds the actor of the call that is appending.
    """
    principal, actor_text = actor_identity(session, explicit)
    fields = {"actor_text": actor_text}
    if principal:
        fields["actor"] = principal
    return fields


def _trail_ready(session: Session) -> bool:
    """Postgres, a revision that includes the trail, and the table present.

    The same three checks as ``config_service._trail_ready``. Anything else
    keeps today's close, which writes no audit row.
    """
    get_bind = getattr(session, "get_bind", None)
    if not callable(get_bind):
        return False
    bind = get_bind()
    if getattr(getattr(bind, "dialect", None), "name", None) != "postgresql":
        return False
    if not trail.trail_ready(session):
        return False
    return (
        session.execute(text("SELECT to_regclass('public.audit_trail')")).scalar()
        is not None
    )


def record_close(
    session: Session,
    *,
    principal_id: Optional[str],
    case_id: str,
    actor_text: str,
    credential_id: Optional[str] = None,
) -> None:
    """Append the close to the trail in the caller's transaction.

    No ``case_audit_logs`` row: a close does not write one today.
    """
    if not _trail_ready(session):
        return
    payload: dict[str, str] = {"action": CLOSE_ACTION, "case_id": case_id}
    if principal_id:
        payload["actor"] = principal_id
    if credential_id:
        payload["credential_id"] = credential_id
    trail.append(
        session,
        payload,
        actor_text=actor_text or CALLER_UNAUTHENTICATED,
    )


# Who this server acts as when it writes a name into a record.
#
# A caller that supplies its own name is not identifying itself, it is choosing
# what the record will say, so the name comes from the credential the door
# bound: the /mcp surface, or chat through the principal token
# /internal/tools/invoke verifies. A call with no person behind it binds nothing.
def actor() -> str:
    """Who this call writes into a record. Never an argument the model supplied."""
    return current_caller() or CALLER_UNAUTHENTICATED


@contextmanager
def service_session() -> Iterator:
    """A session of the caller's own, committed if the work returns.

    Tools reach the database directly rather than through the API, so each
    call owns its transaction. One definition, so commit means one thing.
    """
    session = get_db_session()
    try:
        yield session
        session.commit()
    finally:
        session.close()
