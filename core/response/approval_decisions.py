"""What follows an approval decision, for the HTTP route and the chat/MCP tool alike."""

import asyncio
import logging
from typing import Any, Dict, Optional, Tuple

from core.response.approval_service import ApprovalService, PendingAction, case_ids_for

logger = logging.getLogger(__name__)


# The decision on each case the approval is on. Best effort: it is decided either way.
def record_decision(
    action: PendingAction, decided_by: str, reason: Optional[str] = None
) -> None:
    rejected = action.status == "rejected"
    verb = "rejected" if rejected else "approved"
    try:
        from core.workflows.run_bridge_router import _add_activity

        case_ids = case_ids_for(action)
    except Exception:  # noqa: BLE001
        logger.exception("could not resolve the cases of %s", action.action_id)
        return
    for case_id in case_ids:
        try:
            _add_activity(
                case_id,
                f"approval_{verb}",
                f"{action.title} {verb} by {decided_by}"
                + (f": {reason}" if rejected and reason else ""),
                {
                    "action_id": action.action_id,
                    "decided_by": decided_by,
                    "status": action.status,
                    **({"reason": reason} if rejected and reason else {}),
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "could not record the decision on %s on case %s",
                action.action_id,
                case_id,
            )


# A rule promotion or deploy PR (#4068): audit the answer; an approval does the
# move. The decision stands if that fails: the sweep retries an approved one.
async def rollout_decided(
    action: PendingAction, decided_by: str, reason: Optional[str] = None
) -> None:
    from core.response.approval_service import ROLLOUT_ACTION_TYPES

    if action.action_type not in ROLLOUT_ACTION_TYPES:
        return
    from core.detections.inventory import rollout_approval

    try:
        await asyncio.to_thread(rollout_approval.on_decided, action, decided_by, reason)
    except Exception:  # noqa: BLE001
        logger.exception("rollout answer on %s not carried out", action.action_id)


class ActionNotFound(LookupError):
    pass


# The whole of a rejection, for the route and the chat/MCP tool alike: the row,
# the case trail, a rollout's answer, and the paused run it was holding.
# ActionNotFound for no such action; AlreadyDecided passes through; None when the
# row would not update. Returns (action, resume_result).
async def decide_reject(
    service: ApprovalService,
    action_id: str,
    reason: str,
    rejected_by: str,
    principal_id: Optional[str],
) -> Optional[Tuple[PendingAction, Optional[Dict[str, Any]]]]:
    from core.workflows.run_resume import resume_run

    if service.get_action(action_id) is None:
        raise ActionNotFound(action_id)
    updated = service.reject_action(
        action_id,
        reason=reason,
        rejected_by=rejected_by,
        approved_by_principal_id=principal_id,
    )
    if updated is None:
        return None
    record_decision(updated, rejected_by, reason)
    await rollout_decided(updated, rejected_by, reason)
    resume_result = None
    if updated.workflow_run_id:
        resume_result = await resume_run(
            updated.workflow_run_id, action_id, rejected_by
        )
    return updated, resume_result
