"""Intent and outcome around an effect outside Vigil (#4436).

The intent commits on its own session before the call. ``trail.append`` does
not commit, and a request's unit of work commits only when the handler
returns, so the intent is never appended there. The outcome is a second
entry naming that sequence. A missed outcome is left unknown: it is not held.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from core.audit import trail
from core.storage.connection import get_db_session

logger = logging.getLogger(__name__)

JIRA_INTENT = "case.jira.intent"
JIRA_OUTCOME = "case.jira.outcome"
ELASTIC_INTENT = "elastic.alert.intent"
ELASTIC_OUTCOME = "elastic.alert.outcome"
# A 4xx from the outside system. Not a list-filter value.
REFUSED_REMOTELY = "refused remotely"


def recording() -> bool:
    """Whether this database takes trail entries."""
    session = get_db_session()
    try:
        return bool(trail.in_use(session))
    finally:
        session.close()


def http_outcome(status_code: int) -> str:
    """``done`` for 2xx, ``refused remotely`` for 4xx, ``failed`` otherwise."""
    if 200 <= status_code < 300:
        return trail.OUTCOME_DONE
    if 400 <= status_code < 500:
        return REFUSED_REMOTELY
    return trail.OUTCOME_FAILED


def error_outcome(exc: BaseException) -> str:
    """Classify a call that raised. No HTTP status is a transport failure."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return http_outcome(status_code)
    return trail.OUTCOME_FAILED


def _entry(actor: tuple, action: str, **fields: Any) -> tuple[dict, dict]:
    principal, actor_text, on_behalf_of = actor
    payload: dict[str, Any] = {trail.ACTION: action}
    if principal:
        payload[trail.ACTOR] = principal
    if on_behalf_of:
        payload[trail.ON_BEHALF_OF] = on_behalf_of
    payload.update({key: value for key, value in fields.items() if value is not None})
    beside = {"actor_text": actor_text} if actor_text else {}
    return payload, beside


def commit_intent(actor: tuple, action: str, **fields: Any) -> Optional[int]:
    """Commit an awaiting intent. None when the trail is not in use.

    Raises, including ``trail.Paused``, when the trail is in use and the
    entry cannot be committed. The caller must not make the outside call.
    """
    session = get_db_session()
    try:
        if not trail.in_use(session):
            return None
        payload, beside = _entry(actor, action, **fields)
        payload[trail.AWAITING] = True
        entry = trail.append(session, payload, **beside)
        session.commit()
        return entry.seq
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def commit_outcome(
    actor: tuple,
    action: str,
    *,
    intent: int,
    outcome: str,
    **fields: Any,
) -> None:
    """A second entry naming ``intent``. A failure here leaves it unknown."""
    session = get_db_session()
    try:
        if not trail.in_use(session):
            return
        payload, beside = _entry(
            actor, action, intent=str(intent), outcome=outcome, **fields
        )
        # ``_entry`` drops None, then these two are set so a field cannot
        # overwrite the link or the result.
        payload[trail.INTENT] = str(intent)
        payload[trail.OUTCOME] = outcome
        trail.append(session, payload, **beside)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("outside effect outcome was not recorded")
    finally:
        session.close()
