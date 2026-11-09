"""Under Assist, a person's yes before a rule goes live or its deploy PR opens (#4068).

The sweep puts a due promotion to a person as an approval request; a manual
deploy does the same unless the person deploying may approve. Approving carries
the move out, declining leaves the rule where it is. Every step writes a config
audit row naming who did it. Reuses the approvals queue: no table of its own.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

from sqlalchemy import select

from core.response.approval_service import (
    ROLLOUT_ACTION_TYPES,
    ActionStatus,
    ActionType,
    ApprovalService,
    PendingAction,
)
from core.response.ceiling import REVERSIBLE
from core.storage.config_service import write_config_audit
from core.storage.connection import get_db_manager
from core.storage.models import ApprovalAction as ApprovalActionRow
from core.time import utcnow

logger = logging.getLogger(__name__)

AUDIT_TYPE = "detection_rules"
AUDIT_KEY = "rule_approval"
# While one carrier's PR is in flight, no other opens one. Longer than a deploy's
# slowest pass (every call at DEPLOY_TIMEOUT, plus the race waits), so a live
# carrier never loses it; a crashed one's expires and the sweep takes over.
PR_LEASE = timedelta(minutes=10)


def _audit(
    event: str, rule_id: int, who: str, reason: Optional[str], **more: Any
) -> None:
    from core.detections.inventory.sync import trail_actor

    with get_db_manager().session_scope() as session:
        write_config_audit(
            session,
            config_type=AUDIT_TYPE,
            config_key=AUDIT_KEY,
            action=event,
            old_value=None,
            new_value={"rule_id": rule_id, **more},
            changed_by=trail_actor(who),
            change_reason=reason,
        )


def request_promotion(copy: Any, reason: str, proof: Mapping[str, Any]) -> bool:
    """Ask a person to promote ``copy``; True only when this call asked.

    Keyed on the copy's version and when it entered shadow, so every later
    sweep finds the same request, a decline stands for that stay in shadow, and
    a release or new version can ask again.
    """
    action, inserted = ApprovalService()._put_action(
        action_type=ActionType.PROMOTE_RULE,
        title=f"Promote {copy.name} to live",
        description=(
            f"Rule {copy.rule_id} v{copy.version} finished its shadow period. "
            "Approving makes it live and opens its deploy pull request."
        ),
        target=str(copy.rule_id),
        confidence=0.0,
        reason=reason,
        evidence=[],
        parameters={
            "rule_id": copy.rule_id,
            "version": copy.version,
            "proof": dict(proof),
        },
        effect_class=REVERSIBLE,
        idempotency_key=f"promote-rule:{copy.rule_id}:v{copy.version}:"
        f"{copy.stage_at.isoformat()}",
        human_only=True,
        annotate_rule=False,
    )
    if inserted:
        _audit(
            "request",
            copy.rule_id,
            "system",
            reason,
            action_id=action.action_id,
            kind=ActionType.PROMOTE_RULE.value,
            version=copy.version,
        )
    return inserted


def request_deploy(
    copy: Any, requested_by: str, again_after: Optional[str] = None
) -> str:
    """Ask a person to open ``copy``'s deploy PR; the open request if there is one.
    ``again_after``: the closed, unmerged PR a redeploy replaces (#4123)."""
    service = ApprovalService()
    for open_one in service.list_actions(
        status=ActionStatus.PENDING, action_type=ActionType.DEPLOY_RULE
    ):
        params = open_one.parameters or {}
        if (
            params.get("rule_id") == copy.rule_id
            and params.get("version") == copy.version
        ):
            return open_one.action_id
    from core.detections.inventory import rollout

    reason = "Vigil asks before it opens a pull request while it is on Assist"
    state = (
        f"is a Wazuh rule ({rollout.UNTESTED})"
        if copy.language == rollout.WAZUH and copy.stage != "live"
        else "is live"
    )
    title = f"Open the deploy PR for {copy.name}"
    description = f"Rule {copy.rule_id} v{copy.version} {state} and has no deploy PR."
    if again_after:
        title = f"Deploy {copy.name} again"
        description = (
            f"Rule {copy.rule_id} v{copy.version} {state}, but its deploy PR "
            f"{again_after} was closed without merging: open a new PR for it."
        )
    action = service.create_action(
        action_type=ActionType.DEPLOY_RULE,
        title=title,
        description=description,
        target=str(copy.rule_id),
        confidence=0.0,
        reason=reason,
        evidence=[],
        created_by=requested_by,
        parameters={
            "rule_id": copy.rule_id,
            "version": copy.version,
            **({"again_after": again_after} if again_after else {}),
        },
        effect_class=REVERSIBLE,
        human_only=True,
        annotate_rule=False,
    )
    _audit(
        "request",
        copy.rule_id,
        requested_by,
        reason,
        action_id=action.action_id,
        kind=ActionType.DEPLOY_RULE.value,
        version=copy.version,
    )
    return action.action_id


def on_decided(action: PendingAction, who: str, reason: Optional[str] = None) -> None:
    """Audit a person's answer; approving also carries the move out."""
    if action.action_type not in ROLLOUT_ACTION_TYPES:
        return
    rule_id = int((action.parameters or {}).get("rule_id"))
    declined = action.status == ActionStatus.REJECTED.value
    _audit(
        "decline" if declined else "approve",
        rule_id,
        who,
        reason if declined else action.reason,
        action_id=action.action_id,
        kind=action.action_type,
    )
    if not declined:
        from core.detections.inventory.rollout import RolloutReport

        carry_out(action, RolloutReport())


def carry_out_approved(report: Any) -> None:
    """Carry out every approved move not yet done: after Stop lifts, or a PR
    that failed. One failing does not stop the rest."""
    service = ApprovalService()
    for action_type in (ActionType.PROMOTE_RULE, ActionType.DEPLOY_RULE):
        for action in service.list_actions(
            status=ActionStatus.APPROVED, action_type=action_type
        ):
            try:
                carry_out(action, report)
            except Exception:  # noqa: BLE001 -- the next sweep retries
                logger.exception(
                    "rollout approval %s not carried out", action.action_id
                )


def _leased(row: Any) -> bool:
    """Whether another carrier's PR is in flight on ``row``."""
    lease = (row.execution_result or {}).get("pr_lease") or {}
    try:
        return datetime.fromisoformat(lease["until"]) > datetime.now(timezone.utc)
    except (KeyError, TypeError, ValueError):
        return False


@contextmanager
def _claimed(action_id: str) -> Iterator[Tuple[Any, Any]]:
    """(session, the approval row) locked for the caller, or (None, None) when it
    is no longer approved, another carrier is writing it, or another carrier's
    PR is in flight (its ``pr_lease``). The row lock serializes the stage write,
    the lease the PR step, which runs after the lock is released. The lock lasts
    until the transaction ends, so a skipped carrier sees the committed answer."""
    with get_db_manager().session_scope() as session:
        row = session.scalar(
            select(ApprovalActionRow)
            .where(ApprovalActionRow.action_id == action_id)
            .with_for_update(skip_locked=True)
        )
        ok = (
            row is not None
            and row.status == ActionStatus.APPROVED.value
            and not _leased(row)
        )
        yield (session, row) if ok else (None, None)


def _lease(row: Any) -> str:
    """Take the PR lease on a claimed row, in the claim's own transaction."""
    holder = uuid.uuid4().hex
    until = (datetime.now(timezone.utc) + PR_LEASE).isoformat()
    row.execution_result = {"pr_lease": {"holder": holder, "until": until}}
    return holder


def _release(action_id: str, holder: str) -> None:
    """Drop the lease after a PR that did not finish, so the next sweep retries."""
    with get_db_manager().session_scope() as session:
        row = session.get(ApprovalActionRow, action_id, with_for_update=True)
        held = ((row.execution_result or {}).get("pr_lease") or {}) if row else {}
        if row is not None and held.get("holder") == holder:
            row.execution_result = None


def _close(row: Any, status: ActionStatus, result: Dict[str, Any]) -> None:
    row.status = status.value
    row.executed_at = utcnow()
    row.execution_result = result


def _latest_stage(session: Any, rule_id: int) -> Optional[str]:
    from core.detections.inventory import stages

    _rule, latest = stages._state(session, rule_id)
    return latest.stage if latest is not None else None


def _finish(action_id: str, status: ActionStatus, result: Dict[str, Any]) -> None:
    """Close the approval in its own transaction, after the move is done."""
    with get_db_manager().session_scope() as session:
        row = session.get(ApprovalActionRow, action_id, with_for_update=True)
        if row is not None and row.status == ActionStatus.APPROVED.value:
            _close(row, status, result)


def carry_out(action: PendingAction, report: Any) -> bool:
    """Do what an approved request asked. True when it is done; False leaves it
    approved for the sweep to retry (Stop set, or the PR failed), and also when
    another carrier already has it.

    Three steps, never nested: the locked transaction writes the live stage row
    and commits; the PR opens with no lock or transaction held (the audit trail
    takes a lock per write, and a second session waiting on it would wait for
    ever); then the approval is closed in a fresh transaction. The row stays
    approved until the PR is done, so a crash or a failed PR leaves the copy
    live with its PR owed, and the sweep's next pass retries only the PR."""
    from core.detections.inventory import rollout

    if rollout._stopped():
        return False
    with _claimed(action.action_id) as (session, row):
        if row is None:
            return False
        params = row.parameters or {}
        rule_id, version = int(params["rule_id"]), params.get("version")
        who = row.approved_by or "system"
        promote = row.action_type == ActionType.PROMOTE_RULE.value
        # Read after the lock: an earlier carrier's commit is what this sees.
        copy = next((c for c in rollout._copies() if c.rule_id == rule_id), None)
        if copy is None or copy.version != version or copy.held:
            _close(
                row,
                ActionStatus.FAILED,
                {"error": f"rule {rule_id} v{version} is no longer promotable"},
            )
            return False
        if (
            promote
            and copy.stage == "shadow"
            and _latest_stage(session, rule_id) == "shadow"
        ):
            _promote(session, copy, row, who, report)
        elif copy.stage != "live" and (promote or copy.language != rollout.WAZUH):
            # A person may deploy a Wazuh copy whatever its stage (#4062).
            _close(row, ActionStatus.FAILED, {"error": f"rule {rule_id} is not live"})
            return False
        holder = _lease(row)
    try:
        if promote:
            done = _open_pr(copy, who, report)
            result: Dict[str, Any] = {"promoted": True, "pr_opened": done}
        else:
            outcome = rollout.deploy_overlay_only([rule_id], actor=who, approver=True)
            if outcome["skipped"]:
                _finish(
                    action.action_id,
                    ActionStatus.FAILED,
                    {"error": outcome["skipped"][0]["reason"]},
                )
                return False
            done = not outcome["failed"]
            result = {"opened": outcome["opened"], "failed": outcome["failed"]}
        if done:
            _finish(action.action_id, ActionStatus.EXECUTED, result)
        else:
            _release(action.action_id, holder)
        return done
    except Exception:
        # A crash leaves the lease to expire; an error frees it for the retry.
        _release(action.action_id, holder)
        raise


def _promote(session: Any, copy: Any, action: Any, who: str, report: Any) -> None:
    from core.detections.inventory.sync import _insert_stages, _stage_row

    params = action.parameters or {}
    row = {
        **_stage_row(
            copy.rule_id,
            copy.version,
            "live",
            f"approved by {who}: {action.reason}",
        ),
        "actor": who,
        "proof": {**(params.get("proof") or {}), "approval": action.action_id},
    }
    _insert_stages(session, [row], {copy.rule_id: "shadow"})
    copy.stage = "live"
    report.moves.append(f"{copy.rule_id} v{copy.version}: shadow -> live")


def _open_pr(copy: Any, who: str, report: Any) -> bool:
    """Whether nothing is left to open: no target, or a PR (or the file) is there."""
    from core.detections.inventory import rollout

    targets = rollout._deploy_targets()
    if not targets.get(copy.language):
        return True
    if not rollout._linted(copy):
        return False
    if copy.language == "splunk":
        rollout._read_splunk_files([copy])
    if not copy.toml:
        return False
    if not rollout._deploy_due(copy):
        return True
    outcome = rollout._deploy(
        [copy], targets, datetime.now(timezone.utc), report, who
    ).get(copy.rule_id)
    return outcome is not None and "error" not in outcome
