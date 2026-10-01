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
from typing import Any, Dict, Iterable, List, Optional

from opentelemetry.metrics import Observation
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from core.response.config import ResponseConfig, approval_requirement
from core.storage.config_service import get_config_service
from core.storage.connection import get_db_manager
from core.storage.models import ApprovalAction as ApprovalActionRow
from core.storage.models import Investigation, WorkflowRun
from core.telemetry import get_meter
from core.time import utcnow

logger = logging.getLogger(__name__)

APPROVAL_CONFIG_KEY = "approval.force_manual_approval"

_pending_gauge: Any = None


def register_pending_gauge(service: "ApprovalService") -> None:
    """Export the approval queue depth as an observable gauge, once per process.

    Not in ``__init__``: the service is constructed in many places and each
    registration would stack another callback onto the same instrument. The
    caller that owns the process-wide instance (API boot) calls this.
    """
    global _pending_gauge
    if _pending_gauge is not None:
        return

    def _observe(_options: Any) -> Iterable[Observation]:
        try:
            return [Observation(len(service.list_pending_approvals()))]
        except Exception as e:  # a DB outage drops the sample, not the process
            logger.debug("approvals.pending observation failed: %s", e)
            return []

    _pending_gauge = get_meter("vigil.response.approvals").create_observable_gauge(
        "vigil.approvals.pending",
        callbacks=[_observe],
        description="Actions awaiting approval",
        unit="1",
    )


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

    def __init__(self, config: Optional[ResponseConfig] = None):
        """
        Initialize approval service.

        Args:
            config: the confidence band; read from Settings when omitted so
                the no-arg form callers use still honours env (#916).
        """
        self.config = config or ResponseConfig.from_settings()
        self._load_config()

    # ------------------------------------------------------------------
    # Config (force_manual_approval) — db/config-backed, read per decision
    # ------------------------------------------------------------------
    #
    # ``self.force_manual_approval`` is this process forcing approval on
    # (the daemon does when DAEMON_FORCE_APPROVAL is set) and is never
    # written to the row. The row is what Settings writes; it is read at each
    # decision so a long-lived service sees a change without a restart.

    def _load_config(self):
        """Create the stored flag, off, when no row exists yet."""
        self.force_manual_approval = False
        self._last_stored = False
        try:
            config_service = get_config_service()
            value = config_service.get_system_config(APPROVAL_CONFIG_KEY)
            if value:
                self._last_stored = bool(value.get("enabled", False))
            else:
                self._save_default()
        except Exception as e:  # noqa: BLE001
            logger.error("Error loading approval config: %s", e)

    def _stored_force_manual_approval(self) -> bool:
        """The stored flag, or the last one read when the read fails."""
        try:
            value = get_config_service().get_system_config(APPROVAL_CONFIG_KEY)
        except Exception as e:  # noqa: BLE001
            logger.error("Error reading approval config: %s", e)
            value = None
        if value is not None:
            self._last_stored = bool(value.get("enabled", False))
        return self._last_stored

    def _save_default(self):
        """Store the flag off, for an install that has no row yet."""
        try:
            config_value = {"enabled": False}
            config_service = get_config_service(user_id="approval_service")
            config_service.set_system_config(
                key=APPROVAL_CONFIG_KEY,
                value=config_value,
                description="Force manual approval for all actions",
                config_type="approval",
                change_reason="Updated by approval service",
            )
        except Exception as e:  # noqa: BLE001
            logger.error("Error saving approval config: %s", e)

    def set_force_manual_approval(self, force: bool):
        """Force manual approval for this process; the stored row is left as is."""
        self.force_manual_approval = force
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

        # The branch that set requires_approval is appended to the caller's
        # narrative so the row records the rule it was decided by (#917).
        forced = self.force_manual_approval or self._stored_force_manual_approval()
        requires_approval, rule = approval_requirement(
            forced, reversibility, confidence, self.config
        )
        reason = f"{reason}; {rule}" if reason else rule

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


def _text_id(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _case_id_for_action(
    action: PendingAction,
    runs: Dict[str, Any],
    investigations: Dict[str, Optional[str]],
) -> Optional[str]:
    """One case id, in the order #1301 settled. Later sources fill a gap only."""
    params = action.parameters if isinstance(action.parameters, dict) else {}
    direct = _text_id(params.get("case_id"))
    if direct:
        return direct
    if action.workflow_run_id:
        ctx = runs.get(action.workflow_run_id) or {}
        from_run = _text_id(ctx.get("case_id")) if isinstance(ctx, dict) else None
        if from_run:
            return from_run
    investigation_id = _text_id(params.get("investigation_id"))
    if investigation_id:
        return _text_id(investigations.get(investigation_id))
    return None


def pending_approval_case_ids() -> set[str]:
    """Case ids that currently need a person.

    Calls ``list_pending_approvals()`` and does not query ``approval_actions``.
    A case id resolves from ``parameters.case_id``, then
    ``workflow_runs.trigger_context.case_id``, then ``investigations.case_id``.
    """
    actions = ApprovalService().list_pending_approvals()
    if not actions:
        return set()

    run_ids = {action.workflow_run_id for action in actions if action.workflow_run_id}
    investigation_ids: set[str] = set()
    for action in actions:
        params = action.parameters if isinstance(action.parameters, dict) else {}
        investigation_id = _text_id(params.get("investigation_id"))
        if investigation_id:
            investigation_ids.add(investigation_id)
    runs: Dict[str, Any] = {}
    investigations: Dict[str, Optional[str]] = {}
    if run_ids or investigation_ids:
        db = get_db_manager()
        with db.session_scope() as session:
            if run_ids:
                rows = session.query(WorkflowRun).filter(
                    WorkflowRun.run_id.in_(run_ids)
                )
                for row in rows:
                    ctx = row.trigger_context
                    runs[row.run_id] = ctx if isinstance(ctx, dict) else {}
            if investigation_ids:
                rows = session.query(Investigation).filter(
                    Investigation.investigation_id.in_(investigation_ids)
                )
                for row in rows:
                    investigations[row.investigation_id] = row.case_id

    found: set[str] = set()
    for action in actions:
        case_id = _case_id_for_action(action, runs, investigations)
        if case_id:
            found.add(case_id)
    return found
