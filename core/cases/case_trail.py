"""Case and approval actions on the audit trail (#2761).

One append or one ``CaseAuditLog`` insert, in the caller's transaction,
never both: ``trail.in_use`` decides. No revision is added here. The case
Record tab reads both: these entries by ``case_id``, and ``case_audit_logs``.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from core.audit import trail
from core.cases.agent_closure import actor_identity

COMMENT = "case.comment"
COMMENT_UPDATE = "case.comment.update"
COMMENT_DELETE = "case.comment.delete"
EVIDENCE = "case.evidence"
ESCALATE = "case.escalate"
APPROVE = "approval.approve"
REJECT = "approval.reject"


class TrailWriteError(Exception):
    """A trail append failed. A joined caller must not commit past it."""


def _payload(action: str, principal: Optional[str], **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {trail.ACTION: action}
    if principal is not None:
        payload[trail.ACTOR] = principal
    payload.update({k: v for k, v in fields.items() if v is not None})
    return payload


def _beside(actor_text: Optional[str], operator_text: Optional[str]) -> dict:
    beside = {"actor_text": actor_text, "operator_text": operator_text}
    return {k: v for k, v in beside.items() if v is not None}


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

    ``signed_in`` is the username the caller took from the session or from
    ``current_caller()``, never a request body; ``actor_identity`` resolves
    its principal, and an unset caller is ``agent`` with none. ``ids`` runs
    after a flush, so an id the insert assigns is in the hashed payload.
    Previous comment text and an escalation reason go in ``operator_text``.
    """
    # Any failure on the trail path is a TrailWriteError, so a joined caller
    # sees it whatever the cause, not only from the append itself.
    try:
        use_trail = trail.in_use(session)
        if use_trail:
            session.flush()
            principal, actor_text = actor_identity(session, signed_in)
            fields = {"case_id": case_id, **(ids() if ids else {})}
            trail.append(
                session,
                _payload(action, principal, **fields),
                **_beside(actor_text, operator_text),
            )
    except trail.Paused:
        raise  # a restore's pause answers 503 with its retry time, not 500
    except Exception as exc:
        raise TrailWriteError(str(exc)) from exc
    if not use_trail and legacy is not None:
        session.add(legacy)


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

    ``principal_id`` is the one the caller already resolved; ``approved_by``
    and ``approved_by_principal_id`` are not changed. No legacy row:
    approvals had none.
    """
    if not trail.in_use(session):
        return
    trail.append(
        session,
        _payload(action, principal_id, action_id=action_id),
        **_beside(username or None, operator_text),
    )
