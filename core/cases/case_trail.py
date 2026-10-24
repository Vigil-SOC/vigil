"""Case and approval actions on the audit trail (#2761).

One append or one ``CaseAuditLog`` insert, in the caller's transaction,
never both. The choice is the same three checks as
``config_service._trail_ready``: Postgres, the revision, and the table.
No revision is added here. The case Record tab keeps reading
``case_audit_logs`` and does not show these entries.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from sqlalchemy import text

from core.audit import trail
from core.integrations.mcp.surface import current_caller

# What an unbound caller is called. The same word ``caller()`` writes when
# ``current_caller()`` is unset. Not a principal, and not ``agent:<key>``.
UNSET_ACTOR = "agent"

COMMENT = "case.comment"
COMMENT_UPDATE = "case.comment.update"
COMMENT_DELETE = "case.comment.delete"
EVIDENCE = "case.evidence"
ESCALATE = "case.escalate"
APPROVE = "approval.approve"
REJECT = "approval.reject"


def ready(session) -> bool:
    """Whether ``session`` should append instead of inserting ``CaseAuditLog``."""
    get_bind = getattr(session, "get_bind", None)
    if not callable(get_bind):
        return False
    bind = get_bind()
    if getattr(getattr(bind, "dialect", None), "name", None) != "postgresql":
        return False
    if not trail.trail_ready(session):
        return False
    # A create_all database stamped at head has the revision and no table.
    # Appending there would roll the caller's change back.
    return (
        session.execute(text("SELECT to_regclass('public.audit_trail')")).scalar()
        is not None
    )


def _principal(session, username: str) -> Optional[str]:
    found = session.execute(
        text("SELECT principal_id FROM users WHERE username = :username"),
        {"username": username},
    ).scalar()
    return found if isinstance(found, str) and found else None


def resolve_actor(session, signed_in: Optional[str]) -> tuple[Optional[str], str]:
    """``(principal id or None, actor_text)``.

    A bound caller wins: ``current_caller()`` is the signed-in person, looked
    up on ``users`` in this session. Otherwise the username the console
    already took from the session. Neither is a request-body field. An unset
    caller is ``actor_text`` ``agent`` and no principal.
    """
    bound = current_caller()
    if bound and bound.strip():
        name = bound.strip()
    else:
        name = (signed_in or "").strip()
        if not name or name == UNSET_ACTOR:
            return None, UNSET_ACTOR
    return _principal(session, name), name


def _payload(action: str, principal: Optional[str], **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {trail.ACTION: action}
    if principal is not None:
        payload[trail.ACTOR] = principal
    for key, value in fields.items():
        if value is not None:
            payload[key] = value
    return payload


def append_or_legacy(
    session,
    *,
    action: str,
    signed_in: Optional[str],
    case_id: str,
    legacy: Any = None,
    operator_text: Optional[str] = None,
    ids: Optional[Callable[[], dict[str, Any]]] = None,
) -> None:
    """Append, or insert ``legacy`` when the trail is not in use.

    ``ids`` runs after a flush, so a comment, evidence, or escalation id
    assigned by the insert is in the hashed payload. Previous comment text
    and an escalation reason go in ``operator_text``, not the payload.
    """
    if not ready(session):
        if legacy is not None:
            session.add(legacy)
        return
    session.flush()
    principal, actor_text = resolve_actor(session, signed_in)
    fields = {"case_id": case_id, **(ids() if ids else {})}
    beside: dict[str, Any] = {"actor_text": actor_text}
    if operator_text is not None:
        beside["operator_text"] = operator_text
    trail.append(session, _payload(action, principal, **fields), **beside)


def append_approval(
    session,
    *,
    action: str,
    principal_id: Optional[str],
    action_id: str,
    username: Optional[str],
    operator_text: Optional[str] = None,
) -> None:
    """Append an approve or reject in the caller's transaction.

    ``principal_id`` is the one the caller already resolved. This does not
    change ``approved_by`` or ``approved_by_principal_id``. A rejection
    reason sits in ``operator_text``. No legacy row: approvals had none.
    """
    if not ready(session):
        return
    beside: dict[str, Any] = {}
    if username:
        beside["actor_text"] = username
    if operator_text is not None:
        beside["operator_text"] = operator_text
    trail.append(
        session,
        _payload(action, principal_id, action_id=action_id),
        **beside,
    )
