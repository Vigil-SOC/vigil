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
from typing import Any, Dict, Iterable, List, Optional, Sequence

from opentelemetry.metrics import Observation
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from core.cases import case_trail
from core.response.ceiling import IRREVERSIBLE, REVERSIBLE, environment_locks_act
from core.response.config import (
    ResponseConfig,
    approval_requirement,
    decision_rule,
    split_decision_rule,
)
from core.storage.connection import get_db_manager
from core.storage.models import ApprovalAction as ApprovalActionRow
from core.storage.models import Case, Finding, Investigation, WorkflowRun, case_findings
from core.storage.schemas.base import _iso_utc
from core.telemetry import get_meter
from core.time import utcnow

logger = logging.getLogger(__name__)

_pending_gauge: Any = None


class AlreadyDecided(Exception):
    """The row was not pending.

    Raised after the decision transaction commits. ``session_scope`` logs
    every exception as a database failure, and a second decision is not one.
    """

    def __init__(self, action_id: str, status: str):
        super().__init__(f"Action {action_id} is already {status}")


FOUR_EYES_REFUSAL = (
    "Four-eyes is on: someone other than who proposed this action, or the "
    "person its run acted for, must approve it"
)


class FourEyesRefused(Exception):
    """The approver proposed the action, or its run acted for them (#2932).

    Raised after the refusal's trail entry commits; the row stays pending.
    """

    def __init__(self, action_id: str):
        super().__init__(FOUR_EYES_REFUSAL)
        self.action_id = action_id


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


def stored_reversibility(effect_class: Optional[str]) -> str:
    """``approval_actions.reversibility``, which the console's undo fuse reads.

    Two-valued: irreversible for an irreversible or unclassified tool.
    """
    return IRREVERSIBLE if effect_class in (IRREVERSIBLE, None) else REVERSIBLE


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
    # "reversible" or "irreversible": the console's undo fuse reads it.
    reversibility: str = REVERSIBLE
    idempotency_key: Optional[str] = None


def _proposers(session, row: ApprovalActionRow) -> set[str]:
    """The creator's principal and the proposing run's person, when known.

    Hunt proposals keep ``workflow_run_id`` null and name the run in
    ``parameters["run_id"]``.
    """
    found = {row.created_by_principal_id}
    params = row.parameters if isinstance(row.parameters, dict) else {}
    run_id = row.workflow_run_id or _text_id(params.get("run_id"))
    if run_id:
        found.add(
            session.execute(
                select(WorkflowRun.triggered_by_principal_id).where(
                    WorkflowRun.run_id == run_id
                )
            ).scalar_one_or_none()
        )
    found.discard(None)
    return found


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
        reversibility=row.reversibility or REVERSIBLE,
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


def _host_names(*names: Optional[str]) -> List[str]:
    """A host's IP and hostname, folded for comparison, without the "unknown" an
    IP-less finding carries in place of its IP."""
    folded = {n.strip().lower() for n in names if isinstance(n, str) and n.strip()}
    folded.discard("unknown")
    return sorted(folded)


def _isolations_of(names: Sequence[str]):
    """Isolations of a host named by any of ``names``: the auto-responder's, which
    key on the IP when the alert had one and on the hostname when not, and runs'
    proposals, which name the host as the model did."""
    target = func.lower(func.trim(ApprovalActionRow.target))
    hostname = func.lower(func.trim(ApprovalActionRow.parameters["hostname"].astext))
    return (
        select(ApprovalActionRow)
        .where(ApprovalActionRow.action_type == ActionType.ISOLATE_HOST.value)
        .where(
            or_(
                ApprovalActionRow.idempotency_key.like("isolate_host:%"),
                ApprovalActionRow.idempotency_key.like("run-proposal:%"),
            )
        )
        .where(or_(target.in_(names), hostname.in_(names)))
    )


def _hold_hosts(session, names: Sequence[str]) -> None:
    """Hold these host names until the transaction ends, so two alerts about one
    host, one carrying its IP and one not, cannot each find no isolation and file
    their own. PostgreSQL only; other dialects skip the lock."""
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    for name in sorted(names):
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"vigil-isolate:{name}"},
        )


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
        effect_class: Optional[str] = REVERSIBLE,
        idempotency_key: Optional[str] = None,
        human_only: bool = False,
        annotate_rule: bool = True,
    ) -> PendingAction:
        """Create a new pending action.

        Workflow phase approvals pass ``workflow_run_id`` and
        ``workflow_phase_id`` so the approvals UI / resume endpoint can
        link back to the paused run.

        ``effect_class`` is the called tool's stored class, None when it is
        unclassified; a row that names no outside tool stays ``reversible``.
        Every row requires approval. ``human_only`` holds the
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
            effect_class=effect_class,
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
        effect_class: Optional[str] = REVERSIBLE,
        idempotency_key: Optional[str] = None,
        human_only: bool = False,
        annotate_rule: bool = True,
        same_host: Sequence[Optional[str]] = (),
    ) -> tuple[PendingAction, bool]:
        """Insert an approval row, or return the existing non-failed one.

        The bool is True when this call inserted. Isolation uses it so a
        reused approved/pending row is not executed again. ``same_host`` is an
        isolation's IP and hostname: a non-failed isolation of that host under
        either name, or proposed by a run, is the existing row whatever its key.
        """
        key = idempotency_key or None

        # The branch that set requires_approval is appended to the caller's
        # narrative so the row records the rule it was decided by (#917).
        forced = human_only or environment_locks_act()
        requires_approval, rule = approval_requirement(
            forced, effect_class, confidence, self.config
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
                names = _host_names(*same_host)
                if names:
                    _hold_hosts(session, names)
                    existing = session.execute(
                        _isolations_of(names)
                        .where(ApprovalActionRow.status != ActionStatus.FAILED.value)
                        .order_by(ApprovalActionRow.created_at.desc())
                        .limit(1)
                    ).scalar_one_or_none()
                    if existing is not None:
                        return _row_to_pending(existing), False
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
                    reversibility=stored_reversibility(effect_class),
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
        """Approve a pending action. Raises ``AlreadyDecided`` if it is not pending.

        With the ceiling's ``four_eyes`` on, raises ``FourEyesRefused`` when the
        approver created the action or is the person its run acted for. Checked
        here, at approve time, so rows already pending are covered.
        """
        from core.response.ceiling import read_ceiling

        four_eyes = read_ceiling().four_eyes
        try:
            db = get_db_manager()
            decided: Optional[str] = None
            refused = False
            pending: Optional[PendingAction] = None
            with db.session_scope() as session:
                row = session.get(ApprovalActionRow, action_id, with_for_update=True)
                if row is None:
                    logger.warning("Action %s not found", action_id)
                    return None
                if row.status != ActionStatus.PENDING.value:
                    decided = row.status
                elif (
                    four_eyes
                    and approved_by_principal_id is not None
                    and approved_by_principal_id in _proposers(session, row)
                ):
                    refused = True
                    _trail_refusal(session, action_id, approved_by_principal_id)
                else:
                    row.status = ActionStatus.APPROVED.value
                    row.approved_at = utcnow()
                    row.approved_by = approved_by
                    row.approved_by_principal_id = (
                        approved_by_principal_id
                        if approved_by_principal_id is not None
                        else ("system" if approved_by == "system" else None)
                    )
                    session.flush()
                    case_trail.append_approval(
                        session,
                        action=case_trail.APPROVE,
                        principal_id=row.approved_by_principal_id,
                        action_id=action_id,
                        username=approved_by,
                    )
                    pending = _row_to_pending(row)
            if decided is not None:
                logger.warning(
                    "Action %s is not pending (status: %s)",
                    action_id,
                    decided,
                )
                raise AlreadyDecided(action_id, decided)
            if refused:
                logger.warning(
                    "Action %s: four-eyes refused %s", action_id, approved_by
                )
                raise FourEyesRefused(action_id)
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
        """Reject a pending action. Raises ``AlreadyDecided`` if it is not pending."""
        try:
            db = get_db_manager()
            decided: Optional[str] = None
            pending: Optional[PendingAction] = None
            with db.session_scope() as session:
                row = session.get(ApprovalActionRow, action_id, with_for_update=True)
                if row is None:
                    logger.warning("Action %s not found", action_id)
                    return None
                if row.status != ActionStatus.PENDING.value:
                    decided = row.status
                else:
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
                    case_trail.append_approval(
                        session,
                        action=case_trail.REJECT,
                        principal_id=row.approved_by_principal_id,
                        action_id=action_id,
                        username=rejected_by,
                        operator_text=reason,
                    )
                    pending = _row_to_pending(row)
            if decided is not None:
                logger.warning(
                    "Action %s is not pending (status: %s)",
                    action_id,
                    decided,
                )
                raise AlreadyDecided(action_id, decided)
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

    def run_proposal_match(
        self, key: str, action_type: str, target: str
    ) -> Optional[PendingAction]:
        """The row a run's proposal would repeat: its own (by ``key``) first, else
        the newest run proposal of the same action on the same target, any status.
        An isolation also matches the auto-responder's isolation of that host,
        whether its alert named the host by IP or by hostname."""
        db = get_db_manager()
        with db.session_scope() as session:
            own = _nonfailed_by_key(session, key)
            if own is not None:
                return _row_to_pending(own)
            if action_type == ActionType.ISOLATE_HOST.value:
                query = _isolations_of(_host_names(target))
            else:
                query = (
                    select(ApprovalActionRow)
                    .where(ApprovalActionRow.idempotency_key.like("run-proposal:%"))
                    .where(ApprovalActionRow.action_type == action_type)
                    .where(
                        func.lower(func.trim(ApprovalActionRow.target))
                        == target.strip().lower()
                    )
                )
            row = session.execute(
                query.order_by(ApprovalActionRow.created_at.desc()).limit(1)
            ).scalar_one_or_none()
            return _row_to_pending(row) if row else None

    def add_proposal_case(
        self, action_id: str, case_id: str
    ) -> Optional[PendingAction]:
        """List ``case_id`` on a pending proposal, once. None when it is not pending.

        A row that names no case of its own, the auto-responder's, first lists the
        case its alerts resolve to, so joining it does not take it off that case."""
        db = get_db_manager()
        with db.session_scope() as session:
            row = session.execute(
                select(ApprovalActionRow)
                .where(ApprovalActionRow.action_id == action_id)
                .with_for_update()
            ).scalar_one_or_none()
            if row is None or row.status != ActionStatus.PENDING.value:
                return None
            params = dict(row.parameters or {})
            named = proposal_case_ids(params)
            listed = named or case_ids_for(_row_to_pending(row))
            if case_id not in listed or not named:
                params.pop("case_id", None)
                params["case_ids"] = list(dict.fromkeys([*listed, case_id]))
                # A new dict: an in-place change to a JSON column is not flushed.
                row.parameters = params
                session.flush()
            return _row_to_pending(row)


def _trail_refusal(session, action_id: str, approver: str) -> None:
    """One entry per refusal, in the decision's transaction."""
    from core.agents.attribution import append_here
    from core.audit import trail

    append_here(
        session,
        {
            trail.ACTION: "approval.approve",
            trail.ACTOR: approver,
            trail.OBJECT: action_id,
            trail.OUTCOME: trail.OUTCOME_DENIED,
            "reason": "four_eyes",
        },
    )


def _text_id(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def proposal_case_ids(params: Any) -> List[str]:
    """The cases a row names itself: ``case_ids`` in order, else the one ``case_id``
    rows filed before a proposal could be shared."""
    params = params if isinstance(params, dict) else {}
    listed = params.get("case_ids")
    if isinstance(listed, list):
        ids = [one for one in map(_text_id, listed) if one]
        if ids:
            return list(dict.fromkeys(ids))
    direct = _text_id(params.get("case_id"))
    return [direct] if direct else []


def _case_ids_for_action(
    action: PendingAction,
    runs: Dict[str, Any],
    investigations: Dict[str, Optional[str]],
    evidence_cases: Optional[Dict[str, str]] = None,
) -> List[str]:
    """The action's cases, first the one that asked, in the order #1301 settled.
    Later sources fill a gap only, and name one case."""
    params = action.parameters if isinstance(action.parameters, dict) else {}
    direct = proposal_case_ids(params)
    if direct:
        return direct
    if action.workflow_run_id:
        ctx = runs.get(action.workflow_run_id) or {}
        from_run = _text_id(ctx.get("case_id")) if isinstance(ctx, dict) else None
        if from_run:
            return [from_run]
    investigation_id = _text_id(params.get("investigation_id"))
    if investigation_id:
        from_investigation = _text_id(investigations.get(investigation_id))
        if from_investigation:
            return [from_investigation]
    # The responder saves its approval before intake makes the alert's case, so
    # the alert ids in evidence are the only link until then.
    for finding_id in _evidence_ids(action):
        found = (evidence_cases or {}).get(finding_id)
        if found:
            return [found]
    return []


def case_ids_for(action: PendingAction) -> List[str]:
    """One action's cases, resolved as ``needs_you`` resolves them."""
    runs, investigations, evidence_cases, _stored = _case_lookups([action])
    return _case_ids_for_action(action, runs, investigations, evidence_cases)


def _evidence_ids(action: PendingAction) -> List[str]:
    return [one for one in map(_text_id, action.evidence or []) if one]


def _case_lookups(
    actions: List[PendingAction],
) -> tuple[Dict[str, Any], Dict[str, Optional[str]], Dict[str, str], set[str]]:
    """What those actions can name: run trigger contexts, investigation case ids,
    the newest case of each stored alert in their evidence, and those alerts."""
    run_ids = {action.workflow_run_id for action in actions if action.workflow_run_id}
    investigation_ids: set[str] = set()
    for action in actions:
        params = action.parameters if isinstance(action.parameters, dict) else {}
        investigation_id = _text_id(params.get("investigation_id"))
        if investigation_id:
            investigation_ids.add(investigation_id)
    evidence_ids = {one for action in actions for one in _evidence_ids(action)}
    runs: Dict[str, Any] = {}
    investigations: Dict[str, Optional[str]] = {}
    evidence_cases: Dict[str, str] = {}
    stored: set[str] = set()
    if not run_ids and not investigation_ids and not evidence_ids:
        return runs, investigations, evidence_cases, stored
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
        if evidence_ids:
            stored = {
                fid
                for (fid,) in session.query(Finding.finding_id).filter(
                    Finding.finding_id.in_(evidence_ids)
                )
            }
            rows = (
                session.query(case_findings.c.finding_id, Case.case_id, Case.updated_at)
                .join(Case, Case.case_id == case_findings.c.case_id)
                .filter(case_findings.c.finding_id.in_(evidence_ids))
                .all()
            )
            # The newest case, as alert_outcomes._cases_for picks it.
            newest: Dict[str, tuple] = {}
            for finding_id, linked, updated_at in rows:
                key = (updated_at, linked)
                if finding_id not in newest or key > newest[finding_id]:
                    newest[finding_id] = key
            evidence_cases = {fid: key[1] for fid, key in newest.items()}
    return runs, investigations, evidence_cases, stored


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
            case_id = _text_id(ctx.get("case_id"))
            found.append(
                {
                    "kind": "parked",
                    "source_id": row.run_id,
                    "title": PARKED_TITLE,
                    "reason": reason[:1].upper() + reason[1:],
                    # Nothing records when it parked, and its start is not that.
                    "created_at": "",
                    "reversibility": REVERSIBLE,
                    "case_id": case_id,
                    "case_ids": [case_id] if case_id else [],
                    "finding_id": None,
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

    Demo mode has no approval rows. The open cases are what Home lists, and
    opening one is the answer, so this does not read the approval table.
    """
    from core.config import is_demo_mode

    if is_demo_mode():
        from core.findings.demo_reads import load_demo, needs_you_items

        _findings, cases = load_demo()
        return needs_you_items(cases, case_id)
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
    runs, investigations, evidence_cases, stored = _case_lookups(actions)
    wanted = _text_id(case_id)
    items: List[Dict[str, Any]] = []
    for action in actions:
        resolved = _case_ids_for_action(action, runs, investigations, evidence_cases)
        if wanted is not None and wanted not in resolved:
            continue
        items.append(
            {
                "kind": _kind_for_action(action),
                "source_id": action.action_id,
                "title": action.title,
                "reason": split_decision_rule(action.reason)[0],
                "created_at": action.created_at,
                "reversibility": action.reversibility,
                "case_id": resolved[0] if resolved else None,
                "case_ids": resolved,
                # Agent-written evidence is any text: only a stored alert counts.
                "finding_id": next(
                    (one for one in _evidence_ids(action) if one in stored), None
                ),
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
    return {one for item in needs_you()["items"] for one in item["case_ids"]}
