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
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from core.storage.config_service import get_config_service
from core.storage.connection import get_db_manager
from core.storage.models import ApprovalAction as ApprovalActionRow
from core.time import utcnow

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
    WAF_BLOCK = "waf_block"  # Cloudflare WAF IP Access Rule
    GATEWAY_BLOCK = "gateway_block"  # Cloudflare Zero Trust Gateway DNS/HTTP rule
    ACCESS_REVOKE = "access_revoke"  # Cloudflare Zero Trust Access session revoke
    CUSTOM = "custom"


class ActionStatus(Enum):
    """Status of pending actions."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


class Reversibility(Enum):
    """Whether an executed action can be undone."""

    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


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
    reversibility: str = Reversibility.REVERSIBLE.value
    idempotency_key: Optional[str] = None


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
        reversibility=row.reversibility or Reversibility.REVERSIBLE.value,
        idempotency_key=row.idempotency_key,
    )


def _nonfailed_by_key(session, key: str) -> Optional[ApprovalActionRow]:
    """The live row for ``key``, if any. Failed rows are excluded so they can retry."""
    return session.execute(
        select(ApprovalActionRow)
        .where(ApprovalActionRow.idempotency_key == key)
        .where(ApprovalActionRow.status != ActionStatus.FAILED.value)
        .limit(1)
    ).scalar_one_or_none()


class ApprovalService:
    """Service for managing approval workflow for autonomous actions."""

    def __init__(self):
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
        reversibility: Reversibility = Reversibility.REVERSIBLE,
        idempotency_key: Optional[str] = None,
    ) -> PendingAction:
        """Create a new pending action.

        Workflow phase approvals pass ``workflow_run_id`` and
        ``workflow_phase_id`` so the approvals UI / resume endpoint can
        link back to the paused run.

        Irreversible actions always require approval. A second call with
        the same ``idempotency_key`` returns the existing non-failed row.
        """
        action, _inserted = self._put_action(
            action_type=action_type,
            title=title,
            description=description,
            target=target,
            confidence=confidence,
            reason=reason,
            evidence=evidence,
            created_by=created_by,
            parameters=parameters,
            workflow_run_id=workflow_run_id,
            workflow_phase_id=workflow_phase_id,
            reversibility=reversibility,
            idempotency_key=idempotency_key,
        )
        return action

    def _put_action(
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
        reversibility: Reversibility = Reversibility.REVERSIBLE,
        idempotency_key: Optional[str] = None,
    ) -> tuple[PendingAction, bool]:
        """Insert an approval row, or return the existing non-failed one.

        The bool is True when this call inserted. Isolation uses it so a
        reused approved/pending row is not executed again.
        """
        key = idempotency_key or None

        if self.force_manual_approval:
            requires_approval = True
        elif reversibility is Reversibility.IRREVERSIBLE:
            requires_approval = True
        elif reversibility is Reversibility.REVERSIBLE:
            requires_approval = confidence < 0.90
        else:
            raise ValueError(f"Unknown reversibility: {reversibility}")

        action_id = f"action-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
        status = (
            ActionStatus.PENDING.value
            if requires_approval
            else ActionStatus.APPROVED.value
        )

        try:
            db = get_db_manager()
            with db.session_scope() as session:
                if key:
                    existing = _nonfailed_by_key(session, key)
                    if existing is not None:
                        return _row_to_pending(existing), False
                row = ApprovalActionRow(
                    action_id=action_id,
                    action_type=action_type.value,
                    title=title,
                    description=description,
                    target=target,
                    confidence=float(confidence),
                    reason=reason,
                    evidence=list(evidence or []),
                    created_at=utcnow(),
                    created_by=created_by,
                    requires_approval=requires_approval,
                    status=status,
                    parameters=dict(parameters or {}),
                    workflow_run_id=workflow_run_id,
                    workflow_phase_id=workflow_phase_id,
                    reversibility=reversibility.value,
                    idempotency_key=key,
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
            return pending, True
        except IntegrityError:
            if not key:
                raise
            db = get_db_manager()
            with db.session_scope() as session:
                existing = _nonfailed_by_key(session, key)
            if existing is None:
                raise
            return _row_to_pending(existing), False
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

    def list_stale_pending(self, cutoff: datetime) -> List[str]:
        """Ids of pending actions created before ``cutoff``, oldest first.

        Deliberately not expressed as ``list_actions(...)`` filtered in Python:
        that orders newest-first and caps at 500, so once the queue is larger
        than the cap it hides the oldest rows — the exact ones a sweep is for.
        Ids rather than ``PendingAction`` because the caller only rejects them,
        and because ``PendingAction.created_at`` is a serialized string, not a
        datetime to compare against.
        """
        try:
            db = get_db_manager()
            with db.session_scope() as session:
                stmt = (
                    select(ApprovalActionRow.action_id)
                    .where(ApprovalActionRow.status == ActionStatus.PENDING.value)
                    .where(ApprovalActionRow.created_at < cutoff)
                    .order_by(ApprovalActionRow.created_at.asc())
                )
                return list(session.execute(stmt).scalars().all())
        except SQLAlchemyError as e:
            logger.error("DB error listing stale pending actions: %s", e)
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
                row.approved_at = utcnow()
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
                row.approved_at = utcnow()
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
                row.executed_at = utcnow()
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
                row.executed_at = utcnow()
                row.execution_result = {"error": error}
                session.flush()
                logger.error("Action %s failed: %s", action_id, error)
                return _row_to_pending(row)
        except SQLAlchemyError as e:
            logger.error("DB error marking action %s failed: %s", action_id, e)
            return None

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
