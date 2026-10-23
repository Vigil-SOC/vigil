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

from core.response.config import (
    ResponseConfig,
    approval_requirement,
    decision_rule,
    split_decision_rule,
)
from core.storage.config_service import get_config_service
from core.storage.connection import get_db_manager
from core.storage.models import ApprovalAction as ApprovalActionRow
from core.storage.models import Investigation, WorkflowRun
from core.storage.schemas.base import _iso_utc
from core.telemetry import get_meter
from core.time import utcnow

logger = logging.getLogger(__name__)

APPROVAL_CONFIG_KEY = "approval.force_manual_approval"


def read_stored_force_manual_approval() -> bool:
    """The stored flag; Act when no row exists. Raises when the read fails."""
    value = get_config_service().read_system_config(APPROVAL_CONFIG_KEY)
    return bool(value.get("enabled", False)) if isinstance(value, dict) else False


def stored_force_manual_approval() -> bool:
    """The stored flag as the gate applies it: Assist when the read fails."""
    try:
        return read_stored_force_manual_approval()
    except Exception as e:  # noqa: BLE001
        logger.error(
            "Cannot read the approval setting; requiring manual approval: %s", e
        )
        return True


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
        created_at=_iso_utc(row.created_at) if row.created_at else "",
        created_by=row.created_by,
        requires_approval=bool(row.requires_approval),
        status=row.status,
        approved_at=_iso_utc(row.approved_at) if row.approved_at else None,
        approved_by=row.approved_by,
        executed_at=_iso_utc(row.executed_at) if row.executed_at else None,
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
        self.force_manual_approval = False

    # ------------------------------------------------------------------
    # Config (force_manual_approval) — db/config-backed, read per decision
    # ------------------------------------------------------------------
    #
    # ``self.force_manual_approval`` is this process forcing approval on
    # (the daemon does when DAEMON_FORCE_APPROVAL is set) and is never
    # written to the row. The row is what Settings writes and the SQL seed
    # creates (Act); it is read at each decision so a long-lived service sees
    # a change without a restart. Nothing here writes it.

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
        created_by_principal_id: Optional[str] = None,
        parameters: Optional[Dict] = None,
        workflow_run_id: Optional[str] = None,
        workflow_phase_id: Optional[str] = None,
        reversibility: Reversibility = Reversibility.REVERSIBLE,
        idempotency_key: Optional[str] = None,
        human_only: bool = False,
        annotate_rule: bool = True,
    ) -> PendingAction:
        """Create a new pending action.

        Workflow phase approvals pass ``workflow_run_id`` and
        ``workflow_phase_id`` so the approvals UI / resume endpoint can
        link back to the paused run.

        Irreversible actions always require approval. ``human_only`` holds the
        row for a person whatever ``confidence`` says: for callers whose
        confidence is their own claim (an agent, a model's reading of alert
        text) and so cannot be what releases the action. A second call with
        the same ``idempotency_key`` returns the existing non-failed row.
        ``annotate_rule=False`` keeps the deciding rule off ``reason`` for rows
        whose reason is read by an analyst and no confidence was ever compared.
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
            created_by_principal_id=created_by_principal_id,
            parameters=parameters,
            workflow_run_id=workflow_run_id,
            workflow_phase_id=workflow_phase_id,
            reversibility=reversibility,
            idempotency_key=idempotency_key,
            human_only=human_only,
            annotate_rule=annotate_rule,
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
        created_by_principal_id: Optional[str] = None,
        parameters: Optional[Dict] = None,
        workflow_run_id: Optional[str] = None,
        workflow_phase_id: Optional[str] = None,
        reversibility: Reversibility = Reversibility.REVERSIBLE,
        idempotency_key: Optional[str] = None,
        human_only: bool = False,
        annotate_rule: bool = True,
    ) -> tuple[PendingAction, bool]:
        """Insert an approval row, or return the existing non-failed one.

        The bool is True when this call inserted. Isolation uses it so a
        reused approved/pending row is not executed again.
        """
        key = idempotency_key or None

        # The branch that set requires_approval is appended to the caller's
        # narrative so the row records the rule it was decided by (#917).
        forced = (
            human_only or self.force_manual_approval or stored_force_manual_approval()
        )
        requires_approval, rule = approval_requirement(
            forced, reversibility, confidence, self.config
        )
        if human_only:
            rule = decision_rule("approval.human_only", True)
        if annotate_rule:
            reason = f"{reason}; {rule}" if reason else rule

        action_id = f"action-{utcnow().strftime('%Y%m%d-%H%M%S-%f')}"
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
                if created_by_principal_id is None and created_by == "system":
                    created_by_principal_id = "system"
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
                    created_by_principal_id=created_by_principal_id,
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
        approved_by_principal_id: Optional[str] = None,
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
                row.approved_by_principal_id = (
                    approved_by_principal_id
                    if approved_by_principal_id is not None
                    else ("system" if approved_by == "system" else None)
                )
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
        approved_by_principal_id: Optional[str] = None,
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
                row.approved_by_principal_id = (
                    approved_by_principal_id
                    if approved_by_principal_id is not None
                    else ("system" if rejected_by == "system" else None)
                )
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


def _case_lookups(
    actions: List[PendingAction],
) -> tuple[Dict[str, Any], Dict[str, Optional[str]]]:
    """Run trigger contexts and investigation case ids those actions can name."""
    run_ids = {action.workflow_run_id for action in actions if action.workflow_run_id}
    investigation_ids: set[str] = set()
    for action in actions:
        params = action.parameters if isinstance(action.parameters, dict) else {}
        investigation_id = _text_id(params.get("investigation_id"))
        if investigation_id:
            investigation_ids.add(investigation_id)
    runs: Dict[str, Any] = {}
    investigations: Dict[str, Optional[str]] = {}
    if not run_ids and not investigation_ids:
        return runs, investigations
    db = get_db_manager()
    with db.session_scope() as session:
        if run_ids:
            rows = session.query(WorkflowRun).filter(WorkflowRun.run_id.in_(run_ids))
            for row in rows:
                ctx = row.trigger_context
                runs[row.run_id] = ctx if isinstance(ctx, dict) else {}
        if investigation_ids:
            rows = session.query(Investigation).filter(
                Investigation.investigation_id.in_(investigation_ids)
            )
            for row in rows:
                investigations[row.investigation_id] = row.case_id
    return runs, investigations


def _kind_for_action(action: PendingAction) -> str:
    params = action.parameters if isinstance(action.parameters, dict) else {}
    checkpoint = params.get("checkpoint_id")
    if isinstance(checkpoint, str):
        return "checkpoint" if checkpoint.strip() else "approval"
    return "checkpoint" if checkpoint else "approval"


def _checkpoint_fields(action: PendingAction) -> Dict[str, Any]:
    """What a checkpoint's decision block shows beyond its title.

    The checkpoint id ties the item to the run's own open checkpoint, so a page
    reading both can tell the question it is showing has been answered. For a
    hypothesis approval, the explanations being approved, each with where it came
    from: the benign one is the claim the others are tested against.
    """
    params = action.parameters if isinstance(action.parameters, dict) else {}
    checkpoint = params.get("checkpoint_id")
    fields: Dict[str, Any] = {
        "checkpoint_id": checkpoint if isinstance(checkpoint, str) else None,
        "checkpoint_class": params.get("checkpoint_class") or None,
        "hypotheses": [],
    }
    listed = params.get("hypotheses")
    if isinstance(listed, list):
        fields["hypotheses"] = [
            {
                "hypothesis_id": str(one.get("hypothesis_id") or ""),
                "statement": str(one.get("statement") or ""),
                "provenance": str(one.get("provenance") or ""),
            }
            for one in listed
            if isinstance(one, dict) and one.get("statement")
        ]
    return fields


# How a hunt says it is waiting on a checkpoint rather than stopped at a limit: the
# reason its worker posts (services/agent plugins/hunt book.ts suspendedBecause and
# its "awaiting " park reason). A checkpoint wait is already an approval row.
_CHECKPOINT_WAITS = ("waiting for your approval", "awaiting ")

# Kinds whose run parks at its own limits and takes Keep going (an extend directive).
_EXTENDABLE_KINDS = ("hunt", "adjudicate")

PARKED_TITLE = "Keep the hunt going?"


def _parked_runs(pending_runs: set[str]) -> List[Dict[str, Any]]:
    """Hunts stopped at a limit -- turns, time or budget -- and waiting on a person.

    Nothing else asks for them: there is no approval row behind a parked hunt, so
    the case read "Needs you 0" while it sat there. A run with a pending approval
    is that approval's, and a run waiting on a checkpoint is not at a limit.
    """
    db = get_db_manager()
    with db.session_scope() as session:
        rows = (
            session.query(WorkflowRun)
            .filter(WorkflowRun.status == "paused")
            .filter(WorkflowRun.deleted_at.is_(None))
            .order_by(WorkflowRun.started_at.asc())
            .all()
        )
        found = []
        for row in rows:
            ctx = row.trigger_context if isinstance(row.trigger_context, dict) else {}
            reason = (row.reason or "").strip()
            if row.run_id in pending_runs or not reason:
                continue
            if ctx.get("run_kind") not in _EXTENDABLE_KINDS:
                continue
            if reason.lower().startswith(_CHECKPOINT_WAITS):
                continue
            found.append(
                {
                    "kind": "parked",
                    "source_id": row.run_id,
                    "title": PARKED_TITLE,
                    "reason": reason[:1].upper() + reason[1:],
                    # Nothing records when it parked, and its start is not that.
                    "created_at": "",
                    "reversibility": Reversibility.REVERSIBLE.value,
                    "case_id": _text_id(ctx.get("case_id")),
                    "run_id": row.run_id,
                    "checkpoint_id": None,
                    "checkpoint_class": None,
                    "hypotheses": [],
                }
            )
        return found


def needs_you(case_id: Optional[str] = None) -> Dict[str, Any]:
    """Pending rows that need a person, oldest first, with no cap, then the hunts
    stopped at a limit.

    Not ``list_pending_approvals`` or ``list_actions``: those are newest-first,
    and ``list_actions`` stops at 500, which hides the oldest asks.
    """
    db = get_db_manager()
    with db.session_scope() as session:
        stmt = (
            select(ApprovalActionRow)
            .where(ApprovalActionRow.status == ActionStatus.PENDING.value)
            .where(ApprovalActionRow.requires_approval.is_(True))
            .order_by(ApprovalActionRow.created_at.asc())
        )
        actions = [
            _row_to_pending(row) for row in session.execute(stmt).scalars().all()
        ]
    runs, investigations = _case_lookups(actions)
    wanted = _text_id(case_id)
    items: List[Dict[str, Any]] = []
    for action in actions:
        resolved = _case_id_for_action(action, runs, investigations)
        if wanted is not None and resolved != wanted:
            continue
        items.append(
            {
                "kind": _kind_for_action(action),
                "source_id": action.action_id,
                "title": action.title,
                "reason": split_decision_rule(action.reason)[0],
                "created_at": action.created_at,
                "reversibility": action.reversibility,
                "case_id": resolved,
                "run_id": action.workflow_run_id,
                **_checkpoint_fields(action),
            }
        )
    pending_runs = {action.workflow_run_id for action in actions}
    for parked in _parked_runs({run for run in pending_runs if run}):
        if wanted is not None and parked["case_id"] != wanted:
            continue
        items.append(parked)
    return {"count": len(items), "items": items}


def needs_you_case_ids() -> set[str]:
    """Case ids that need a person, from one ``needs_you()`` read."""
    return {item["case_id"] for item in needs_you()["items"] if item["case_id"]}
