"""Under Assist, a person's yes before a rule goes live or its deploy PR opens (#4068).

The sweep puts a due promotion to a person as an approval request; a manual
deploy does the same unless the person deploying may approve. Approving carries
the move out, declining leaves the rule where it is. Every step writes a config
audit row naming who did it. Reuses the approvals queue: no table of its own.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
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


def _audit(
    event: str, rule_id: int, who: str, reason: Optional[str], **more: Any
) -> None:
    with get_db_manager().session_scope() as session:
        write_config_audit(
            session,
            config_type=AUDIT_TYPE,
            config_key=AUDIT_KEY,
            action=event,
            old_value=None,
            new_value={"rule_id": rule_id, **more},
            changed_by=who,
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
            kind="promote",
            version=copy.version,
        )
    return inserted


def request_deploy(copy: Any, requested_by: str) -> str:
    """Ask a person to open ``copy``'s deploy PR; the open request if there is one."""
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
    reason = "Vigil asks before it opens a pull request while it is on Assist"
    action = service.create_action(
        action_type=ActionType.DEPLOY_RULE,
        title=f"Open the deploy PR for {copy.name}",
        description=f"Rule {copy.rule_id} v{copy.version} is live and has no deploy PR.",
        target=str(copy.rule_id),
        confidence=0.0,
        reason=reason,
        evidence=[],
        created_by=requested_by,
        parameters={"rule_id": copy.rule_id, "version": copy.version},
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
        kind="deploy",
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


@contextmanager
def _claimed(action_id: str) -> Iterator[Tuple[Any, Any]]:
    """(session, the approval row) locked for the caller, or (None, None) when it
    is no longer approved or another carrier holds it. The row is the one
    serialization point: whoever holds it promotes and opens the PR, and a
    second carrier skips instead of repeating either. The lock lasts until the
    transaction ends, so the answer a skipped carrier sees is the committed one."""
    with get_db_manager().session_scope() as session:
        row = session.scalar(
            select(ApprovalActionRow)
            .where(ApprovalActionRow.action_id == action_id)
            .with_for_update(skip_locked=True)
        )
        ok = row is not None and row.status == ActionStatus.APPROVED.value
        yield (session, row) if ok else (None, None)


def _close(row: Any, status: ActionStatus, result: Dict[str, Any]) -> None:
    row.status = status.value
    row.executed_at = utcnow()
    row.execution_result = result


def _latest_stage(session: Any, rule_id: int) -> Optional[str]:
    from core.detections.inventory import stages

    _rule, latest = stages._state(session, rule_id)
    return latest.stage if latest is not None else None


def carry_out(action: PendingAction, report: Any) -> bool:
    """Do what an approved request asked. True when it is done; False leaves it
    approved for the sweep to retry (Stop set, or the PR failed), and also when
    another carrier already has it."""
    from core.detections.inventory import rollout

    if rollout._stopped():
        return False
    with _claimed(action.action_id) as (session, row):
        if row is None:
            return False
        params = row.parameters or {}
        rule_id, version = int(params["rule_id"]), params.get("version")
        who = row.approved_by or "system"
        # Read after the lock: an earlier carrier's commit is what this sees.
        copy = next((c for c in rollout._copies() if c.rule_id == rule_id), None)
        if copy is None or copy.version != version or copy.held:
            _close(
                row,
                ActionStatus.FAILED,
                {"error": f"rule {rule_id} v{version} is no longer promotable"},
            )
            return False
        promote = row.action_type == ActionType.PROMOTE_RULE.value
        if (
            promote
            and copy.stage == "shadow"
            and (_latest_stage(session, rule_id) == "shadow")
        ):
            _promote(session, copy, row, who, report)
        elif copy.stage != "live":
            _close(row, ActionStatus.FAILED, {"error": f"rule {rule_id} is not live"})
            return False
        if promote:
            done = _open_pr(copy, who, report)
            result: Dict[str, Any] = {"promoted": True, "pr_opened": done}
        else:
            outcome = rollout.deploy_overlay_only([rule_id], actor=who, approver=True)
            if outcome["skipped"]:
                _close(
                    row,
                    ActionStatus.FAILED,
                    {"error": outcome["skipped"][0]["reason"]},
                )
                return False
            done = not outcome["failed"]
            result = {"opened": outcome["opened"], "failed": outcome["failed"]}
        if done:
            _close(row, ActionStatus.EXECUTED, result)
        return done


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
