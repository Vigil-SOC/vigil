"""Sign-in, account and credential events on the audit trail (#2760).

Every function calls ``core.audit.trail``; none writes the trail itself. A
database the upgrade has not reached keeps today's behaviour: each function
is a no-op until ``trail.trail_ready`` is true there. A ``create_all``
database stamped at head has that revision and no ``audit_trail`` table, so
the table has to be there too; otherwise the insert rolls the caller's
change back.

``record`` joins the caller's transaction, so a trail failure rolls the action
back. ``safety`` is for an action that goes ahead regardless (late hold).
``refused`` and ``sign_in_failed`` commit on their own session, because the
request transaction rolls back with the 401 or 403 they precede, and they
never turn that refusal into a different error.

Never pass a secret, a password, a token, a hash of one, an MFA secret, a
recovery code or a credential label.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

from sqlalchemy import Connection, text
from sqlalchemy.orm import Session

from core.audit import trail
from core.storage.unit_of_work import unit_of_work

logger = logging.getLogger(__name__)

# Databases whose revision includes the trail and whose table is present.
# The answer never goes back to false for a database, so only a true one is
# remembered. A stamp with no table is not remembered: the table may arrive.
_ready: set[str] = set()


def ready(session=None) -> bool:
    with unit_of_work(session) as session:
        # A stand-in session (unit tests) or another dialect has no trail.
        if not isinstance(session, (Session, Connection)):
            return False
        bind = session.get_bind() if isinstance(session, Session) else session
        if bind.dialect.name != "postgresql":
            return False
        key = str(bind.engine.url)
        if key not in _ready and trail.trail_ready(session) and _table_present(session):
            _ready.add(key)
        return key in _ready


def _table_present(session) -> bool:
    """``stamp_schema_head`` names the head revision on a database ``create_all`` built."""
    return (
        session.execute(text("SELECT to_regclass('public.audit_trail')")).scalar()
        is not None
    )


def payload(action: str, **fields: Any) -> dict[str, Any]:
    """The hashed document: ``action`` and whichever ids the event has."""
    return {"action": action, **{k: v for k, v in fields.items() if v is not None}}


def personal(user) -> dict:
    """An account's name and address, for the ``personal`` beside-column."""
    return {"username": user.username, "email": user.email, "full_name": user.full_name}


def record(session, action: str, beside: Optional[dict] = None, **fields: Any):
    """Append in the caller's transaction."""
    if ready(session):
        trail.append(session, payload(action, **fields), **_beside(beside))


def safety(action: str, beside: Optional[dict] = None, **fields: Any) -> None:
    """After a safety action has happened: appended, or held to record late."""
    try:
        if not ready():
            return
    except Exception:  # noqa: BLE001 - nowhere to hold it either; the action stands
        logger.exception("audit trail unreachable; %s not recorded", action)
        return
    trail.append_or_hold(payload(action, **fields), **_beside(beside))


def refused(action: str, beside: Optional[dict] = None, **fields: Any) -> None:
    """A denial, committed on its own; the refusal stands if this fails."""
    try:
        with unit_of_work() as session:
            record(session, action, beside, **fields)
    except Exception:  # noqa: BLE001 - the caller still refuses
        logger.exception("audit trail did not record %s", action)


def sign_in_failed(
    source_ip: Optional[str],
    attempted_name: Optional[str],
    reason: str,
    principal: Optional[str] = None,
) -> None:
    """Collapsed on source address and attempted name, known account or not."""
    name = (attempted_name or "")[: trail.ATTEMPTED_NAME_MAX]
    try:
        with unit_of_work() as session:
            if ready(session):
                trail.collapse(
                    session,
                    trail.failure_key(source_ip, name),
                    payload("auth.sign_in_failed", reason=reason, principal=principal),
                    source_ip=source_ip,
                    attempted_name=name,
                )
    except Exception:  # noqa: BLE001 - still the generic 401 or 423
        logger.exception("audit trail did not record a failed sign-in")


def credential_expired(credential_id: str) -> None:
    """The first presentation of an expired credential, once.

    A held marker under the credential's window key, ``count`` 0, says it was
    recorded: ``close_windows`` skips a zero count and ``drain_late`` skips a
    window key, so the marker never becomes an entry of its own.
    """
    key = json.dumps(["credential_expiry", credential_id])
    marker = payload("credential.expired", actor=trail.SYSTEM)
    try:
        with unit_of_work() as session:
            if not ready(session):
                return
            session.execute(
                text(
                    "SELECT 1 FROM mcp_credentials WHERE credential_id = :id FOR UPDATE"
                ),
                {"id": credential_id},
            )
            first = session.execute(
                text("""
                    INSERT INTO public.audit_trail_held
                        (window_key, occurred_at, count, payload)
                    VALUES (:key, now(), 0, CAST(:payload AS jsonb))
                    ON CONFLICT (window_key) DO NOTHING
                    RETURNING id
                """),
                {
                    "key": hashlib.sha256(key.encode("utf-8")).hexdigest(),
                    "payload": json.dumps(marker),
                },
            ).first()
            if first is not None:
                trail.append(session, {**marker, "credential_id": credential_id})
    except Exception:  # noqa: BLE001 - an expired credential is refused regardless
        logger.exception(
            "audit trail did not record credential %s expiring", credential_id
        )


def credential_inactive(credential_id: str, principal: Optional[str]) -> None:
    """A credential presented for a deactivated account, once per window."""
    try:
        with unit_of_work() as session:
            if ready(session):
                trail.collapse(
                    session,
                    json.dumps(["credential_inactive", credential_id]),
                    payload(
                        "credential.refused",
                        reason="account_inactive",
                        principal=principal,
                        credential_id=credential_id,
                    ),
                )
    except Exception:  # noqa: BLE001 - refused regardless
        logger.exception(
            "audit trail did not record credential %s refused", credential_id
        )


def _beside(beside: Optional[dict]) -> dict:
    return {k: v for k, v in (beside or {}).items() if v is not None}
