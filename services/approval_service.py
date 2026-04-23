"""Approval service for managing pending autonomous actions.

Actions are persisted in the ``approval_actions`` table (#128). Prior
to that migration, pending actions lived in ``data/pending_actions.json``
— fine for the daemon's single-process loop but invisible to the API
and with no FK into workflow runs. The DB move gives us a queryable,
joinable surface that links workflow phase approvals back to the run
they paused.

Public API (``create_action``, ``approve_action``, ``reject_action``,
``mark_executed``, ``mark_failed``, ``list_actions``, ``get_action``)
is intentionally preserved so ``daemon/orchestrator.py`` (and any
other existing callers) keep working.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional
import sys

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

sys.path.insert(0, str(Path(__file__).parent.parent))
from database.config_service import get_config_service
from database.connection import get_db_manager
from database.models import ApprovalAction as ApprovalActionRow

logger = logging.getLogger(__name__)


class ActionType(Enum):
    """Types of actions that can be approved."""

    ISOLATE_HOST = "isolate_host"
    BLOCK_IP = "block_ip"
    BLOCK_DOMAIN = "block_domain"
    QUARANTINE_FILE = "quarantine_file"
    DISABLE_USER = "disable_user"
    EXECUTE_SPL_QUERY = "execute_spl_query"
    WORKFLOW_PHASE = "workflow_phase"  # #128 — phase with approval_required=True
    CUSTOM = "custom"


class ActionStatus(Enum):
    """Status of pending actions."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


@dataclass
class PendingAction:
    """Represents a pending action awaiting approval.

    Kept as a dataclass for API-stable serialisation; populated from
    ``ApprovalAction`` ORM rows by ``_row_to_pending``.
    """

    action_id: str
    action_type: str  # ActionType value
    title: str
    description: str
    target: str  # IP, hostname, username, etc.
    confidence: float
    reason: str
    evidence: List[str]
    created_at: str
    created_by: str
    requires_approval: bool
    status: str  # ActionStatus value
    approved_at: Optional[str] = None
    approved_by: Optional[str] = None
    executed_at: Optional[str] = None
    execution_result: Optional[Dict] = None
    rejection_reason: Optional[str] = None
    parameters: Optional[Dict] = None
    # #128 — workflow phase approvals link back here.
    workflow_run_id: Optional[str] = None
    workflow_phase_id: Optional[str] = None


def _row_to_pending(row: ApprovalActionRow) -> PendingAction:
    return PendingAction(
        action_id=row.action_id,
        action_type=row.action_type,
        title=row.title,
        description=row.description,
        target=row.target,
        confidence=float(row.confidence or 0),
        reason=row.reason,
        evidence=list(row.evidence or []),
        created_at=row.created_at.isoformat() if row.created_at else "",
        created_by=row.created_by,
        requires_approval=bool(row.requires_approval),
        status=row.status,
        approved_at=row.approved_at.isoformat() if row.approved_at else None,
        approved_by=row.approved_by,
        executed_at=row.executed_at.isoformat() if row.executed_at else None,
        execution_result=row.execution_result,
        rejection_reason=row.rejection_reason,
        parameters=dict(row.parameters or {}),
        workflow_run_id=row.workflow_run_id,
        workflow_phase_id=row.workflow_phase_id,
    )


class ApprovalService:
    """Service for managing approval workflow for autonomous actions."""

    def __init__(self, data_dir: Optional[Path] = None, dry_run: bool = False):
        """
        Initialize approval service.

        Args:
            data_dir: retained for backwards compatibility with callers
                that previously passed a data directory; ignored now
                that storage lives in Postgres.
            dry_run: If True, don't execute actions, just log them
        """
        self.dry_run = dry_run
        # data_dir retained as attribute so any caller introspecting
        # it doesn't break; no filesystem I/O is performed anymore.
        self.data_dir = data_dir
        self._load_config()

    # ------------------------------------------------------------------
    # Config (force_manual_approval) — unchanged, still db/config-backed
    # ------------------------------------------------------------------

    def _load_config(self):
        """Load approval configuration from database."""
        try:
            config_service = get_config_service()
            config_value = config_service.get_system_config(
                "approval.force_manual_approval"
            )
            if config_value:
                self.force_manual_approval = config_value.get("enabled", False)
                logger.debug(
                    "Loaded approval config: force_manual_approval=%s",
                    self.force_manual_approval,
                )
            else:
                self.force_manual_approval = False
                self._save_config()
        except Exception as e:  # noqa: BLE001
            logger.error("Error loading approval config: %s", e)
            self.force_manual_approval = False

    def _save_config(self):
        """Save approval configuration to database."""
        try:
            config_value = {"enabled": self.force_manual_approval}
            config_service = get_config_service(user_id="approval_service")
            config_service.set_system_config(
                key="approval.force_manual_approval",
                value=config_value,
                description="Force manual approval for all actions",
                config_type="approval",
                change_reason="Updated by approval service",
            )
        except Exception as e:  # noqa: BLE001
            logger.error("Error saving approval config: %s", e)

    def set_force_manual_approval(self, force: bool):
        """Set whether to force manual approval for all actions."""
        self.force_manual_approval = force
        self._save_config()
        logger.info("Force manual approval set to: %s", force)

    def get_force_manual_approval(self) -> bool:
        """Get the current force manual approval setting."""
        return self.force_manual_approval

    def should_auto_approve(
        self,
        action: Dict,
        threshold: float = 0.90,
        force_manual: bool = False,
    ) -> bool:
        """Decide if an action should auto-approve based on confidence."""
        if force_manual or self.get_force_manual_approval():
            return False
        confidence = action.get("confidence", 0.0)
        if confidence >= threshold:
            return True
        if confidence >= 0.85:
            return True
        return False

    def needs_flag(self, confidence: float) -> bool:
        """Check if an action needs a flag (confidence 0.85-0.89)."""
        return 0.85 <= confidence < 0.90

    def get_action_decision(self, action: Dict, threshold: float = 0.90) -> str:
        """Get the decision for an action based on confidence."""
        confidence = action.get("confidence", 0.0)
        if confidence < 0.70:
            return "monitor_only"
        elif confidence < 0.85:
            return "manual_approval"
        else:
            return "auto_approve"

    def is_valid_action_type(self, action_type: str) -> bool:
        """Check if an action type is valid."""
        try:
            ActionType(action_type)
            return True
        except ValueError:
            return False

    def validate_action(self, action: Dict) -> tuple[bool, List[str]]:
        """Validate an action payload."""
        errors = []
        required_fields = ["type", "target", "confidence"]
        for field in required_fields:
            if field not in action:
                errors.append(f"Missing required field: {field}")
        if "type" in action and not self.is_valid_action_type(action["type"]):
            errors.append(f"Invalid action type: {action['type']}")
        if "confidence" in action:
            confidence = action.get("confidence", 0.0)
            if not (0.0 <= confidence <= 1.0):
                errors.append(
                    f"Confidence must be between 0.0 and 1.0, got {confidence}"
                )
        return (len(errors) == 0, errors)

    # ------------------------------------------------------------------
    # CRUD — DB-backed
    # ------------------------------------------------------------------

    def create_action(
        self,
        action_type: ActionType,
        title: str,
        description: str,
        target: str,
        confidence: float,
        reason: str,
        evidence: List[str],
        created_by: str = "system",
        parameters: Optional[Dict] = None,
        workflow_run_id: Optional[str] = None,
        workflow_phase_id: Optional[str] = None,
    ) -> PendingAction:
        """Create a new pending action.

        Workflow phase approvals pass ``workflow_run_id`` and
        ``workflow_phase_id`` so the approvals UI / resume endpoint can
        link back to the paused run.
        """
        if self.force_manual_approval:
            requires_approval = True
        else:
            requires_approval = confidence < 0.90

        action_id = f"action-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
        status = (
            ActionStatus.PENDING.value
            if requires_approval
            else ActionStatus.APPROVED.value
        )

        try:
            db = get_db_manager()
            with db.session_scope() as session:
                row = ApprovalActionRow(
                    action_id=action_id,
                    action_type=action_type.value,
                    title=title,
                    description=description,
                    target=target,
                    confidence=float(confidence),
                    reason=reason,
                    evidence=list(evidence or []),
                    created_at=datetime.utcnow(),
                    created_by=created_by,
                    requires_approval=requires_approval,
                    status=status,
                    parameters=dict(parameters or {}),
                    workflow_run_id=workflow_run_id,
                    workflow_phase_id=workflow_phase_id,
                )
                session.add(row)
                session.flush()
                pending = _row_to_pending(row)
            logger.info(
                "Created action %s: %s (confidence: %s)",
                action_id,
                title,
                confidence,
            )
            return pending
        except SQLAlchemyError as e:
            logger.error("DB error creating action: %s", e)
            raise

    def get_action(self, action_id: str) -> Optional[PendingAction]:
        """Get a specific action by ID."""
        try:
            db = get_db_manager()
            with db.session_scope() as session:
                row = session.get(ApprovalActionRow, action_id)
                return _row_to_pending(row) if row else None
        except SQLAlchemyError as e:
            logger.error("DB error fetching action %s: %s", action_id, e)
            return None

    def list_actions(
        self,
        status: Optional[ActionStatus] = None,
        action_type: Optional[ActionType] = None,
        requires_approval: Optional[bool] = None,
        workflow_run_id: Optional[str] = None,
        limit: int = 500,
    ) -> List[PendingAction]:
        """List actions with optional filters, newest first."""
        try:
            db = get_db_manager()
            with db.session_scope() as session:
                stmt = select(ApprovalActionRow)
                if status:
                    stmt = stmt.where(ApprovalActionRow.status == status.value)
                if action_type:
                    stmt = stmt.where(
                        ApprovalActionRow.action_type == action_type.value
                    )
                if requires_approval is not None:
                    stmt = stmt.where(
                        ApprovalActionRow.requires_approval == requires_approval
                    )
                if workflow_run_id:
                    stmt = stmt.where(
                        ApprovalActionRow.workflow_run_id == workflow_run_id
                    )
                stmt = stmt.order_by(ApprovalActionRow.created_at.desc()).limit(limit)
                rows = session.execute(stmt).scalars().all()
                return [_row_to_pending(r) for r in rows]
        except SQLAlchemyError as e:
            logger.error("DB error listing actions: %s", e)
            return []

    def approve_action(
        self,
        action_id: str,
        approved_by: str = "analyst",
    ) -> Optional[PendingAction]:
        """Approve a pending action."""
        try:
            db = get_db_manager()
            with db.session_scope() as session:
                row = session.get(ApprovalActionRow, action_id)
                if row is None:
                    logger.warning("Action %s not found", action_id)
                    return None
                if row.status != ActionStatus.PENDING.value:
                    logger.warning(
                        "Action %s is not pending (status: %s)",
                        action_id,
                        row.status,
                    )
                    return _row_to_pending(row)
                row.status = ActionStatus.APPROVED.value
                row.approved_at = datetime.utcnow()
                row.approved_by = approved_by
                session.flush()
                pending = _row_to_pending(row)
            logger.info("Action %s approved by %s", action_id, approved_by)
            return pending
        except SQLAlchemyError as e:
            logger.error("DB error approving action %s: %s", action_id, e)
            return None

    def reject_action(
        self,
        action_id: str,
        reason: str,
        rejected_by: str = "analyst",
    ) -> Optional[PendingAction]:
        """Reject a pending action."""
        try:
            db = get_db_manager()
            with db.session_scope() as session:
                row = session.get(ApprovalActionRow, action_id)
                if row is None:
                    logger.warning("Action %s not found", action_id)
                    return None
                if row.status != ActionStatus.PENDING.value:
                    logger.warning(
                        "Action %s is not pending (status: %s)",
                        action_id,
                        row.status,
                    )
                    return _row_to_pending(row)
                row.status = ActionStatus.REJECTED.value
                row.rejection_reason = reason
                row.approved_by = rejected_by
                row.approved_at = datetime.utcnow()
                session.flush()
                pending = _row_to_pending(row)
            logger.info(
                "Action %s rejected by %s: %s",
                action_id,
                rejected_by,
                reason,
            )
            return pending
        except SQLAlchemyError as e:
            logger.error("DB error rejecting action %s: %s", action_id, e)
            return None

    def mark_executed(
        self,
        action_id: str,
        result: Dict,
    ) -> Optional[PendingAction]:
        """Mark an action as executed."""
        try:
            db = get_db_manager()
            with db.session_scope() as session:
                row = session.get(ApprovalActionRow, action_id)
                if row is None:
                    return None
                if row.status != ActionStatus.APPROVED.value:
                    logger.warning(
                        "Action %s is not approved (status: %s)",
                        action_id,
                        row.status,
                    )
                    return _row_to_pending(row)
                row.status = ActionStatus.EXECUTED.value
                row.executed_at = datetime.utcnow()
                row.execution_result = result
                session.flush()
                return _row_to_pending(row)
        except SQLAlchemyError as e:
            logger.error("DB error marking action %s executed: %s", action_id, e)
            return None

    def mark_failed(
        self,
        action_id: str,
        error: str,
    ) -> Optional[PendingAction]:
        """Mark an action as failed."""
        try:
            db = get_db_manager()
            with db.session_scope() as session:
                row = session.get(ApprovalActionRow, action_id)
                if row is None:
                    return None
                row.status = ActionStatus.FAILED.value
                row.executed_at = datetime.utcnow()
                row.execution_result = {"error": error}
                session.flush()
                logger.error("Action %s failed: %s", action_id, error)
                return _row_to_pending(row)
        except SQLAlchemyError as e:
            logger.error("DB error marking action %s failed: %s", action_id, e)
            return None

    def get_pending_count(self) -> int:
        """Get count of pending actions requiring approval."""
        return len(self.list_pending_approvals())

    def get_stats(self) -> Dict:
        """Get statistics about actions."""
        actions = self.list_actions()
        return {
            "total": len(actions),
            "pending": len(
                [a for a in actions if a.status == ActionStatus.PENDING.value]
            ),
            "approved": len(
                [a for a in actions if a.status == ActionStatus.APPROVED.value]
            ),
            "rejected": len(
                [a for a in actions if a.status == ActionStatus.REJECTED.value]
            ),
            "executed": len(
                [a for a in actions if a.status == ActionStatus.EXECUTED.value]
            ),
            "failed": len(
                [a for a in actions if a.status == ActionStatus.FAILED.value]
            ),
            "requires_approval": len([a for a in actions if a.requires_approval]),
            "by_type": self._count_by_type(actions),
        }

    def _count_by_type(self, actions: List[PendingAction]) -> Dict[str, int]:
        """Count actions by type."""
        counts: Dict[str, int] = {}
        for action in actions:
            counts[action.action_type] = counts.get(action.action_type, 0) + 1
        return counts

    def list_pending_approvals(self) -> List[PendingAction]:
        """List all pending actions requiring approval."""
        return self.list_actions(status=ActionStatus.PENDING, requires_approval=True)

    def get_audit_trail(self, action_id: str) -> List[Dict]:
        """Get audit trail for a specific action."""
        action = self.get_action(action_id)
        if not action:
            return []

        trail = [
            {
                "event": "created",
                "timestamp": action.created_at,
                "user": action.created_by,
                "details": {
                    "action_type": action.action_type,
                    "target": action.target,
                    "confidence": action.confidence,
                },
            }
        ]

        if action.approved_at:
            if action.status in [
                ActionStatus.APPROVED.value,
                ActionStatus.EXECUTED.value,
            ]:
                trail.append(
                    {
                        "event": "approved",
                        "timestamp": action.approved_at,
                        "user": action.approved_by,
                        "details": {},
                    }
                )
            elif action.status == ActionStatus.REJECTED.value:
                trail.append(
                    {
                        "event": "rejected",
                        "timestamp": action.approved_at,
                        "user": action.approved_by,
                        "details": {"reason": action.rejection_reason},
                    }
                )

        if action.executed_at:
            trail.append(
                {
                    "event": (
                        "executed"
                        if action.status == ActionStatus.EXECUTED.value
                        else "failed"
                    ),
                    "timestamp": action.executed_at,
                    "user": "system",
                    "details": {"result": action.execution_result},
                }
            )

        return trail

    def execute_action(self, action: Dict) -> Dict:
        """Execute an action (with dry run support)."""
        if self.dry_run:
            logger.info(
                "DRY RUN: Would execute %s on %s",
                action.get("type"),
                action.get("target"),
            )
            return {
                "status": "dry_run",
                "would_execute": True,
                "action": action,
            }
        logger.warning(
            "Action execution not yet fully implemented: %s",
            action.get("type"),
        )
        return {
            "status": "not_implemented",
            "message": "Action execution requires service integration",
            "action": action,
        }

    def execute_approved_action(self, action_id: str) -> Dict:
        """Execute an approved action by ID."""
        action = self.get_action(action_id)
        if not action:
            return {"error": f"Action {action_id} not found"}
        if action.status != ActionStatus.APPROVED.value:
            return {
                "error": (
                    f"Action {action_id} is not approved " f"(status: {action.status})"
                )
            }
        action_dict = {
            "type": action.action_type,
            "target": action.target,
            "confidence": action.confidence,
            "parameters": action.parameters,
        }
        result = self.execute_action(action_dict)
        if result.get("status") == "success":
            self.mark_executed(action_id, result)
        elif result.get("status") not in ["dry_run", "not_implemented"]:
            self.mark_failed(action_id, result.get("error", "Unknown error"))
        return result

    def add_to_queue(self, action: Dict) -> str:
        """Add an action to the approval queue (wraps create_action)."""
        is_valid, errors = self.validate_action(action)
        if not is_valid:
            raise ValueError(f"Invalid action: {', '.join(errors)}")
        pending_action = self.create_action(
            action_type=ActionType(action["type"]),
            title=action.get("title", f"{action['type']}: {action['target']}"),
            description=action.get("description", action.get("reasoning", "")),
            target=action["target"],
            confidence=action["confidence"],
            reason=action.get("reasoning", action.get("reason", "")),
            evidence=action.get("evidence", []),
            created_by=action.get("created_by", "system"),
            parameters=action.get("parameters"),
            workflow_run_id=action.get("workflow_run_id"),
            workflow_phase_id=action.get("workflow_phase_id"),
        )
        return pending_action.action_id

    def log_approval_decision(
        self,
        action: Dict,
        decision: str,
        user: str,
        reasoning: Optional[str] = None,
    ) -> Dict:
        """Log an approval decision."""
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "action_type": action.get("type"),
            "target": action.get("target"),
            "confidence": action.get("confidence"),
            "decision": decision,
            "user": user,
            "reasoning": reasoning or action.get("reasoning", ""),
            "dry_run": self.dry_run,
        }
        logger.info(
            "Approval decision logged: %s by %s for %s",
            decision,
            user,
            action.get("type"),
        )
        return log_entry

    def log_execution(
        self,
        action_id: str,
        status: str,
        result: Optional[Dict] = None,
        error: Optional[str] = None,
    ) -> Dict:
        """Log action execution result."""
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "action_id": action_id,
            "status": status,
            "result": result,
            "error": error,
            "dry_run": self.dry_run,
        }
        if status == "success":
            logger.info("Action %s executed successfully", action_id)
            if not self.dry_run:
                self.mark_executed(action_id, result or {})
        elif status == "failed":
            logger.error("Action %s failed: %s", action_id, error)
            if not self.dry_run:
                self.mark_failed(action_id, error or "Unknown error")
        else:
            logger.info(
                "Action %s execution skipped (dry run or other reason)",
                action_id,
            )
        return log_entry


_approval_service: Optional[ApprovalService] = None


def get_approval_service() -> ApprovalService:
    """Get singleton ApprovalService instance."""
    global _approval_service
    if _approval_service is None:
        _approval_service = ApprovalService()
    return _approval_service
