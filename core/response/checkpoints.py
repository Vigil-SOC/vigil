# A checkpoint the agent layer parked on, as an approval a human can answer. The
# answer goes back via /internal/runs/{id}/decisions; this side never writes the ledger.

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def pending_for(run_id: str, checkpoint_id: str, approvals=None):
    from core.response.approval_service import ActionStatus, ApprovalService

    service = approvals or ApprovalService()
    for action in service.list_actions(
        status=ActionStatus.PENDING, workflow_run_id=run_id
    ):
        if (action.parameters or {}).get("checkpoint_id") == checkpoint_id:
            return action
    return None


# Idempotent by checkpoint, because a supervisor reads the same open checkpoint on
# every tick: raising per read would queue the same question hundreds of times.
def raise_for_checkpoint(
    *,
    run_id: str,
    checkpoint_id: str,
    title: str,
    description: str,
    reason: str,
    parameters: Optional[Dict[str, Any]] = None,
    phase_id: Optional[str] = None,
    approvals=None,
) -> bool:
    from core.response.approval_service import ActionType, ApprovalService

    service = approvals or ApprovalService()
    if pending_for(run_id, checkpoint_id, service) is not None:
        return False

    service.create_action(
        action_type=ActionType.WORKFLOW_PHASE,
        title=title,
        description=description,
        target=run_id,
        confidence=0.0,
        reason=reason,
        # A checkpoint is always a person's call: no confidence was compared, so
        # no threshold belongs on the row.
        human_only=True,
        annotate_rule=False,
        evidence=[run_id],
        created_by="agent",
        parameters={"checkpoint_id": checkpoint_id, **(parameters or {})},
        workflow_run_id=run_id,
        workflow_phase_id=phase_id,
    )
    logger.info("raised approval for %s at checkpoint %s", run_id, checkpoint_id)
    return True


# A run that ended is not waiting for anything, and nothing withdrew its question:
# the approvals queue kept filling with questions whose run was over, until a real
# one was buried in them. Rejected rather than deleted, because who asked and why
# it went unanswered is the record.
def withdraw_for_run(run_id: str, reason: str, approvals=None) -> int:
    from core.response.approval_service import ActionStatus, ApprovalService

    service = approvals or ApprovalService()
    open_ones = service.list_actions(
        status=ActionStatus.PENDING, workflow_run_id=run_id
    )
    for action in open_ones:
        service.reject_action(action.action_id, reason, rejected_by="agent")
    if open_ones:
        logger.info("withdrew %d unanswered approvals for %s", len(open_ones), run_id)
    return len(open_ones)


# The run-less siblings of withdraw_for_run. That function keys on
# workflow_run_id, so approvals raised without a run behind them are unreachable
# and nothing else ages them out: the queue only grows and the one question that
# matters ends up buried (#675). It is also a safety line, not housekeeping —
# these are containment proposals, and approving a weeks-old "isolate host" out
# of a cluttered queue acts on a reason that stopped being true. Rejected rather
# than deleted, and attributed to "system", so the record distinguishes the queue
# ageing a question out from an analyst declining it.
def expire_stale(older_than_days: int, approvals=None) -> int:
    from datetime import timedelta

    from core.response.approval_service import ApprovalService
    from core.time import utcnow

    if older_than_days < 1:
        raise ValueError(
            "older_than_days must be at least 1, got "
            f"{older_than_days}: a zero or negative window expires approvals "
            "raised seconds ago, including the one an operator is reading"
        )

    service = approvals or ApprovalService()
    cutoff = utcnow() - timedelta(days=older_than_days)
    reason = f"expired: unanswered for more than {older_than_days} days"
    stale = service.list_stale_pending(cutoff)
    for action_id in stale:
        service.reject_action(action_id, reason, rejected_by="system")
    if stale:
        logger.info(
            "expired %d approvals unanswered since before %s",
            len(stale),
            cutoff.isoformat(),
        )
    return len(stale)
