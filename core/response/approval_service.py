"""Approval service for managing pending autonomous actions.

Actions are persisted in the ``approval_actions`` table (#128). Prior
to that migration, pending actions lived in ``data/pending_actions.json``
— fine for the daemon's single-process loop but invisible to the API
and with no FK into workflow runs. The DB move gives us a queryable,
joinable surface that links workflow phase approvals back to the run
they paused.

Public API (``create_action``, ``approve_action``, ``reject_action``,
``release_action``, ``mark_executed``, ``mark_failed``, ``list_actions``,
``get_action``)
is intentionally preserved so ``daemon/orchestrator.py`` (and any
other existing callers) keep working.
"""

import logging
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Union

from opentelemetry.metrics import Observation
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from core.cases import case_trail
from core.cases.case_status import CLOSED
from core.response.ceiling import (
    IRREVERSIBLE,
    REVERSIBLE,
    environment_locks_act,
    locked_flags,
)
from core.response.config import (
    ResponseConfig,
    approval_requirement,
    decision_rule,
    split_decision_rule,
)
from core.response.proposal_targets import DONE_STATE
from core.storage.connection import get_db_manager
from core.storage.models import ApprovalAction as ApprovalActionRow
from core.storage.models import Case, Finding, Investigation, WorkflowRun, case_findings
from core.storage.schemas.base import _iso_utc
from core.telemetry import get_meter
from core.time import utcnow

logger = logging.getLogger(__name__)

_pending_gauge: Any = None

# Rollout acts on these once approved (rollout_approval).
ROLLOUT_ACTION_TYPES = frozenset({"promote_rule", "deploy_rule"})
# Kinds the backend carries out on approval through a vendor's tool (#5059): each
# with state words, whether or not a carrier is bound for it (#5313). One with
# none bound is still resolved, and stays a proposal.
CONTAINMENT_TYPES = frozenset(DONE_STATE)
# execution_result.state while the call is out: claimed, so it runs once.
RUNNING = "running"
# A claim this old outlived its process: the row may be approved again.
_STALE_RUN = timedelta(minutes=15)


def _stale_run(row: ApprovalActionRow) -> bool:
    result = row.execution_result if isinstance(row.execution_result, dict) else {}
    if result.get("state") != RUNNING:
        return False
    try:
        started = datetime.fromisoformat(result.get("started_at") or "")
    except ValueError:
        return True
    return utcnow() - started >= _STALE_RUN


def runs_here(params: Any) -> bool:
    """A row the backend runs once approved: it carries a tool and is no run's
    checkpoint, which its own run calls when it resumes."""
    params = params if isinstance(params, dict) else {}
    tool = params.get("tool")
    return isinstance(tool, str) and bool(tool) and not params.get("checkpoint_id")


# runs_here, as a query filter: what a stop holds (#5059).
def _runs_here_clause():
    params = ApprovalActionRow.parameters
    return (func.coalesce(params["tool"].astext, "") != "") & (
        func.coalesce(params["checkpoint_id"].astext, "") == ""
    )


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


class ActionNotFound(LookupError):
    """No such action, or one on a restricted case the caller can't read (#4359)."""


class NotHeld(Exception):
    """The row is not an approved action a stop is holding."""

    def __init__(self, action_id: str):
        super().__init__(f"Action {action_id} is not held")
        self.action_id = action_id


class NotUndoable(Exception):
    """The row is not an executed containment in its done state, or a lift of
    it is still open (#5060)."""


def _open_lift(session, action_id: str) -> Optional[str]:
    """Why ``action_id`` can't be lifted again: a row undoing it is still open."""
    rows = session.execute(
        select(ApprovalActionRow)
        .where(ApprovalActionRow.parameters["undoes"].astext == action_id)
        .where(
            or_(
                ApprovalActionRow.status == ActionStatus.APPROVED.value,
                (ApprovalActionRow.status == ActionStatus.EXECUTED.value)
                & (ApprovalActionRow.execution_result["state"].astext == "requested"),
            )
        )
    ).scalars()
    # A claim that outlived its process does not hold the way back shut.
    open_id = next((r.action_id for r in rows if not _stale_run(r)), None)
    return f"{open_id} is already undoing {action_id}" if open_id else None


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
    REVOKE_SESSION = "revoke_session"  # end an account's sessions (#5234)
    EXECUTE_SPL_QUERY = "execute_spl_query"
    WORKFLOW_PHASE = "workflow_phase"  # #128 — phase with approval_required=True
    PROMOTE_RULE = "promote_rule"  # shadow -> live under Assist (#4068)
    DEPLOY_RULE = "deploy_rule"  # open a live rule's deploy PR under Assist (#4068)
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
    # The step score's value (#5278), None for a row without one. The stored
    # column held the proposer's claim and is no longer read.
    confidence: Optional[float]
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
    # Set while a stop holds this approved outside action.
    held: bool = False
    # Set at read (#4359): a placeholder for a non-reader, or why it waits.
    restricted: bool = False
    waiting: Optional[str] = None


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


def four_eyes_refuses(
    session, run_id: str, checkpoint_id: Any, principal_id: Optional[str]
) -> bool:
    """Whether four-eyes refuses ``principal_id`` an approval of the run's checkpoint.

    The run's directive inbox answers the same pending row the Approvals screen
    does, so it is held to approve_action's rule. With no such row (not written
    yet, withdrawn, or a hunt's own checkpoint) the run's person still proposed
    it, so they are refused as they would be on the row.
    """
    from core.response.ceiling import read_ceiling

    if principal_id is None or not read_ceiling().four_eyes:
        return False
    rows = session.execute(
        select(ApprovalActionRow)
        .where(ApprovalActionRow.status == ActionStatus.PENDING.value)
        .where(ApprovalActionRow.workflow_run_id == run_id)
    ).scalars()
    for row in rows:
        params = row.parameters if isinstance(row.parameters, dict) else {}
        if params.get("checkpoint_id") == checkpoint_id:
            return principal_id in _proposers(session, row)
    started_by = session.execute(
        select(WorkflowRun.triggered_by_principal_id).where(
            WorkflowRun.run_id == run_id
        )
    ).scalar_one_or_none()
    return started_by == principal_id


def _step_value(parameters: Any) -> Optional[float]:
    """``parameters.step_score.value``, or None for a row without a score."""
    score = parameters.get("step_score") if isinstance(parameters, dict) else None
    value = score.get("value") if isinstance(score, dict) else None
    return float(value) if isinstance(value, (int, float)) else None


def _decider_score(parameters: Any) -> Optional[Dict[str, Any]]:
    """What the person deciding sees: the quote plus the raise line and factors."""
    from core.response.step_confidence import quote

    score = parameters.get("step_score") if isinstance(parameters, dict) else None
    if not isinstance(score, dict) or not score:
        return None
    return {
        **quote(score),
        "raise": score.get("raise"),
        "factors": score.get("factors"),
    }


def _row_to_pending(row: ApprovalActionRow) -> PendingAction:
    return PendingAction(
        action_id=row.action_id,
        action_type=row.action_type,
        title=row.title,
        description=row.description,
        target=row.target,
        confidence=_step_value(row.parameters),
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
        held=bool(row.held),
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


def _asks():
    """Rows that ask for an act, whoever filed them: a run, a person, a playbook or
    the retired daemon responder. An undo row (``undoes``) asks for the way back,
    and a run's question (``workflow_run_id``) is answered by resuming that run, so
    neither is an ask for the act itself."""
    return (
        select(ApprovalActionRow)
        .where(ApprovalActionRow.parameters["undoes"].astext.is_(None))
        .where(ApprovalActionRow.workflow_run_id.is_(None))
    )


def _isolations_of(names: Sequence[str]):
    """Asks to isolate a host named by any of ``names``: by its IP or its hostname,
    as the retired responder keyed on either and a run names the host as the model
    did."""
    target = func.lower(func.trim(ApprovalActionRow.target))
    hostname = func.lower(func.trim(ApprovalActionRow.parameters["hostname"].astext))
    return (
        _asks()
        .where(ApprovalActionRow.action_type == ActionType.ISOLATE_HOST.value)
        .where(or_(target.in_(names), hostname.in_(names)))
    )


def _same_ask(action_type: str, target: str):
    """Asks for ``action_type`` on ``target``, any status, whoever filed them; for
    an isolation, any isolation of that host."""
    if action_type == ActionType.ISOLATE_HOST.value:
        return _isolations_of(_host_names(target))
    return (
        _asks()
        .where(ApprovalActionRow.action_type == action_type)
        .where(
            func.lower(func.trim(ApprovalActionRow.target)) == target.strip().lower()
        )
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
        # None reads the band live (env plus an applied INTENT.md revision).
        self._config = config

    @property
    def config(self) -> ResponseConfig:
        return self._config or ResponseConfig.from_settings()

    # ------------------------------------------------------------------
    # CRUD — DB-backed
    # ------------------------------------------------------------------

    def create_action(
        self,
        action_type: ActionType,
        title: str,
        description: str,
        target: str,
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
        Every row requires approval. ``human_only`` records that the row is
        a person's call whatever the ceiling allows (an agent's ask, a model's
        reading of alert text). A second call with the same
        ``idempotency_key`` returns the existing non-failed row.
        ``annotate_rule=False`` keeps the deciding rule off ``reason`` for rows
        whose reason is read by an analyst.
        """
        action, _inserted = self._put_action(
            action_type=action_type,
            title=title,
            description=description,
            target=target,
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
        isolation's IP and hostname: an isolation of that host under either
        name, asked for by anyone and in any status but failed (a rejected one
        included), is the existing row whatever its key.
        """
        key = idempotency_key or None

        # The branch that set requires_approval is appended to the caller's
        # narrative so the row records the rule it was decided by (#917).
        forced = human_only or environment_locks_act()
        requires_approval, rule = approval_requirement(
            forced, effect_class, self.config
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
                if status == ActionStatus.PENDING.value:
                    _trail_request(session, row, effect_class)
            logger.info("Created action %s: %s", action_id, title)
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
        held: Optional[bool] = None,
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
                if held is not None:
                    stmt = stmt.where(ApprovalActionRow.held.is_(held))
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
        call: Optional[tuple[str, str]] = None,
    ) -> Optional[PendingAction]:
        """Approve a pending action. Raises ``AlreadyDecided`` if it is not pending.

        A failed containment may be approved again. ``call`` is the tool and
        JSON arguments the approval runs, set on the row as it is approved.
        With the ceiling's ``four_eyes`` on, raises ``FourEyesRefused`` when the
        approver created the action or is the person its run acted for. Checked
        here, at approve time, so rows already pending are covered. An approval
        while stopped holds a row the backend would run.
        """
        try:
            db = get_db_manager()
            decided: Optional[str] = None
            refused = False
            pending: Optional[PendingAction] = None
            with db.session_scope() as session:
                # Ceiling first, then the action row: stop locks them in that order.
                stopped, four_eyes = locked_flags(session)
                row = session.get(ApprovalActionRow, action_id, with_for_update=True)
                if row is None:
                    logger.warning("Action %s not found", action_id)
                    return None
                _refuse_unreadable(row)
                retry = row.action_type in CONTAINMENT_TYPES and (
                    row.status == ActionStatus.FAILED.value
                    or (row.status == ActionStatus.APPROVED.value and _stale_run(row))
                )
                if row.status != ActionStatus.PENDING.value and not retry:
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
                    if call is not None:
                        # A new dict: an in-place change to a JSON column is not flushed.
                        row.parameters = {
                            **(row.parameters or {}),
                            "tool": call[0],
                            "args": call[1],
                        }
                    if stopped and runs_here(row.parameters):
                        row.held = True
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
        """Reject a pending action, or an approved one a stop is holding.

        Raises ``AlreadyDecided`` when the row is neither.
        """
        try:
            db = get_db_manager()
            decided: Optional[str] = None
            pending: Optional[PendingAction] = None
            with db.session_scope() as session:
                row = session.get(ApprovalActionRow, action_id, with_for_update=True)
                if row is None:
                    logger.warning("Action %s not found", action_id)
                    return None
                _refuse_unreadable(row)
                held_approved = row.status == ActionStatus.APPROVED.value and row.held
                if row.status != ActionStatus.PENDING.value and not held_approved:
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
                    if row.action_type in CONTAINMENT_TYPES:
                        from core.response.reversals import write_reversal_note

                        # A person's rejection is remembered and holds the target (#5273).
                        write_reversal_note(
                            session,
                            row,
                            by=rejected_by,
                            principal_id=row.approved_by_principal_id,
                            reason=reason,
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

    def release_action(
        self,
        action_id: str,
        released_by: str,
        principal_id: Optional[str] = None,
    ) -> Optional[PendingAction]:
        """Clear the stop's hold. The row stays approved. Raises ``NotHeld``.

        The caller runs it (core/response/containment.py).
        """
        db = get_db_manager()
        not_held = False
        pending: Optional[PendingAction] = None
        with db.session_scope() as session:
            row = session.get(ApprovalActionRow, action_id, with_for_update=True)
            if row is None:
                logger.warning("Action %s not found", action_id)
                return None
            _refuse_unreadable(row)
            if row.status != ActionStatus.APPROVED.value or not row.held:
                not_held = True
            else:
                row.held = False
                session.flush()
                case_trail.append_approval(
                    session,
                    action=case_trail.RELEASE,
                    principal_id=_release_actor(principal_id, released_by),
                    action_id=action_id,
                    username=released_by,
                )
                pending = _row_to_pending(row)
        if not_held:
            raise NotHeld(action_id)
        logger.info("Action %s released by %s", action_id, released_by)
        return pending

    def release_held(
        self, released_by: str, principal_id: Optional[str] = None
    ) -> List[PendingAction]:
        """Clear the hold on every held row this caller may decide.

        One trail entry per row. A row on a restricted case they can't read
        stays held.
        """
        db = get_db_manager()
        released: List[PendingAction] = []
        actor = _release_actor(principal_id, released_by)
        with db.session_scope() as session:
            rows = (
                session.execute(
                    select(ApprovalActionRow)
                    .where(ApprovalActionRow.status == ActionStatus.APPROVED.value)
                    .where(ApprovalActionRow.held.is_(True))
                    .where(_runs_here_clause())
                    .order_by(ApprovalActionRow.created_at.asc())
                    .with_for_update()
                )
                .scalars()
                .all()
            )
            for row in rows:
                if not may_decide(_row_to_pending(row)):
                    continue
                row.held = False
                session.flush()
                case_trail.append_approval(
                    session,
                    action=case_trail.RELEASE,
                    principal_id=actor,
                    action_id=row.action_id,
                    username=released_by,
                )
                released.append(_row_to_pending(row))
        return released

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
        result: Optional[Dict] = None,
    ) -> Optional[PendingAction]:
        """Mark an action as failed. ``result`` is recorded beside the error."""
        try:
            db = get_db_manager()
            with db.session_scope() as session:
                row = session.get(ApprovalActionRow, action_id)
                if row is None:
                    return None
                row.status = ActionStatus.FAILED.value
                row.executed_at = utcnow()
                row.execution_result = {**(result or {}), "error": error}
                session.flush()
                logger.error("Action %s failed: %s", action_id, error)
                return _row_to_pending(row)
        except SQLAlchemyError as e:
            logger.error("DB error marking action %s failed: %s", action_id, e)
            return None

    def update_row(
        self,
        action_id: str,
        status: str,
        parameters: Optional[Dict] = None,
        execution_result: Optional[Dict] = None,
        held: Optional[bool] = None,
        reversibility: Optional[str] = None,
    ) -> Optional[PendingAction]:
        """Merge into a row's parameters or execution result, or set its hold or
        reversibility, if it is still in ``status``. None when it is not (#5059)."""
        db = get_db_manager()
        with db.session_scope() as session:
            row = session.get(ApprovalActionRow, action_id, with_for_update=True)
            if row is None or row.status != status:
                return None
            if parameters:
                row.parameters = {**(row.parameters or {}), **parameters}
            if execution_result:
                row.execution_result = {
                    **(row.execution_result or {}),
                    **execution_result,
                }
            if held is not None:
                row.held = held
            if reversibility is not None:
                row.reversibility = reversibility
            session.flush()
            return _row_to_pending(row)

    def create_undo(
        self,
        action_id: str,
        build: Callable[[PendingAction], Union[str, tuple[str, Dict[str, Any]]]],
        approved_by: str,
        approved_by_principal_id: Optional[str],
        reason: Optional[str] = None,
    ) -> PendingAction:
        """Insert the row that undoes an executed containment, approved by the
        caller (#5060), with their ``reason`` on it and in an analyst note (#5273). ``build`` turns the original into the row's title and
        parameters, or says why it can't. No proposal is involved, so four-eyes
        does not apply.

        Raises ``NotUndoable`` unless the original is executed and in its done
        state with no lift of it still open; ``ActionNotFound`` as approving does.
        """

        found = self.get_action(action_id)
        if found is None:
            raise ActionNotFound(action_id)
        case_ids = case_ids_for(found)
        refusal: Optional[str] = None
        pending: Optional[PendingAction] = None
        db = get_db_manager()
        # A refusal is raised after the transaction: session_scope logs any
        # exception inside it as a database failure.
        with db.session_scope() as session:
            stopped, _four_eyes = locked_flags(session)
            # The original's lock serialises lifts: a double click inserts one.
            row = session.get(ApprovalActionRow, action_id, with_for_update=True)
            if row is None:
                raise ActionNotFound(action_id)
            _refuse_unreadable(row)
            original = _row_to_pending(row)
            state = (original.execution_result or {}).get("state")
            done = DONE_STATE.get(original.action_type)
            built = None
            if done is None or (original.parameters or {}).get("undoes"):
                refusal = f"{action_id} is not a containment Vigil can undo"
            elif original.status != ActionStatus.EXECUTED.value or state != done:
                refusal = (
                    f"{action_id} is {state or original.status}, not {done}: "
                    "there is nothing to undo"
                )
            else:
                refusal = _open_lift(session, action_id)
            if refusal is None:
                built = build(original)
                if isinstance(built, str):
                    refusal = built
            if refusal is None and isinstance(built, tuple):
                title, parameters = built
                now = utcnow()
                undo = ApprovalActionRow(
                    action_id=f"action-{now.strftime('%Y%m%d-%H%M%S-%f')}",
                    action_type=original.action_type,
                    title=title,
                    description=f"Undoes {action_id}: {original.title}",
                    target=original.target,
                    reason=f"{approved_by} undid {action_id}"
                    + (f": {reason.strip()}" if (reason or "").strip() else ""),
                    evidence=list(original.evidence or []),
                    created_at=now,
                    created_by=approved_by,
                    created_by_principal_id=approved_by_principal_id,
                    requires_approval=True,
                    status=ActionStatus.APPROVED.value,
                    approved_at=now,
                    approved_by=approved_by,
                    approved_by_principal_id=approved_by_principal_id,
                    # The original's cases, named outright: readers and the trail follow them.
                    parameters={
                        **parameters,
                        "undoes": action_id,
                        "case_ids": case_ids,
                    },
                    reversibility=row.reversibility,
                    # A stop holds it, as it holds any approval that carries a call.
                    held=stopped,
                )
                session.add(undo)
                session.flush()
                case_trail.append_approval(
                    session,
                    action=case_trail.APPROVE,
                    principal_id=approved_by_principal_id,
                    action_id=undo.action_id,
                    username=approved_by,
                )
                from core.response.proposal_targets import UNDONE_STATE
                from core.response.reversals import write_reversal_note

                write_reversal_note(
                    session,
                    row,
                    by=approved_by,
                    principal_id=approved_by_principal_id,
                    reason=reason,
                    undo_verb=UNDONE_STATE.get(original.action_type, "undid"),
                )
                pending = _row_to_pending(undo)
        if refusal is not None or pending is None:
            raise NotUndoable(refusal or f"{action_id} could not be undone")
        logger.info(
            "Action %s undoes %s, by %s", pending.action_id, action_id, approved_by
        )
        return pending

    def claim_run(self, action_id: str, by: str) -> Optional[PendingAction]:
        """Mark an approved, unheld row running, once. None when it is not one,
        or another call already holds it."""
        db = get_db_manager()
        with db.session_scope() as session:
            row = session.get(ApprovalActionRow, action_id, with_for_update=True)
            if (
                row is None
                or row.status != ActionStatus.APPROVED.value
                or row.held
                or (row.execution_result or {}).get("state") == RUNNING
            ):
                return None
            row.execution_result = {
                "state": RUNNING,
                "by": by,
                "started_at": utcnow().isoformat(),
            }
            session.flush()
            return _row_to_pending(row)

    def unclaim_run(self, action_id: str, held: bool) -> Optional[PendingAction]:
        """Drop a running claim on a row still approved, holding it if ``held``."""
        db = get_db_manager()
        with db.session_scope() as session:
            row = session.get(ApprovalActionRow, action_id, with_for_update=True)
            if row is None or row.status != ActionStatus.APPROVED.value:
                return None
            row.execution_result = None
            row.held = held
            session.flush()
            return _row_to_pending(row)

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
        the newest live ask (pending, approved or executed) for the same action on
        the same target, else the newest of any status, whoever filed it: another
        run, a person, or the retired responder. An isolation matches the host by
        IP or by hostname."""
        db = get_db_manager()
        with db.session_scope() as session:
            own = _nonfailed_by_key(session, key)
            if own is not None:
                return _row_to_pending(own)
            # A live ask first: a newer rejected or failed one does not hide it.
            live = ApprovalActionRow.status.in_(_LIVE)
            row = session.execute(
                _same_ask(action_type, target)
                .order_by(live.desc(), ApprovalActionRow.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            return _row_to_pending(row) if row else None

    def add_proposal_case(
        self, action_id: str, case_id: str
    ) -> Optional[PendingAction]:
        """List ``case_id`` on a pending proposal, once. None when it is not pending.

        A row that names no case of its own, a retired responder's, first lists the
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


def _release_actor(principal_id: Optional[str], username: str) -> Optional[str]:
    if principal_id is not None:
        return principal_id
    return "system" if username == "system" else None


def stamp_held_approvals(session) -> None:
    """Mark approved rows the backend runs held, in the caller's transaction."""
    session.execute(
        update(ApprovalActionRow)
        .where(ApprovalActionRow.status == ActionStatus.APPROVED.value)
        .where(_runs_here_clause())
        .values(held=True)
    )


def held_for_ceiling() -> List[dict]:
    """Approved outside actions a stop is still holding, as the ceiling read shows them."""
    db = get_db_manager()
    with db.session_scope() as session:
        rows = (
            session.execute(
                select(ApprovalActionRow)
                .where(ApprovalActionRow.status == ActionStatus.APPROVED.value)
                .where(ApprovalActionRow.held.is_(True))
                .where(_runs_here_clause())
                .order_by(
                    ApprovalActionRow.approved_at.asc().nulls_last(),
                    ApprovalActionRow.created_at.asc(),
                )
            )
            .scalars()
            .all()
        )
        actions = [_row_to_pending(row) for row in rows]
    return [_held_payload(action) for action in screen_actions(actions)]


def _held_payload(action: PendingAction) -> dict:
    if action.restricted:
        return {
            "action_id": action.action_id,
            "action_type": "",
            "title": action.title,
            "target": "",
            "approved_by": None,
            "approved_at": None,
            "restricted": True,
        }
    return {
        "action_id": action.action_id,
        "action_type": action.action_type,
        "title": action.title,
        "target": action.target,
        "approved_by": action.approved_by,
        "approved_at": action.approved_at,
        "restricted": False,
    }


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


def _trail_request(
    session, row: ApprovalActionRow, effect_class: Optional[str]
) -> None:
    """The pending row as an entry, in its insert's transaction. A run's row is
    the run parking on it."""
    from core.audit import trail

    if not trail.in_use(session):
        return
    case_ids = proposal_case_ids(row.parameters)
    if not case_ids and row.workflow_run_id:
        run = session.get(WorkflowRun, row.workflow_run_id)
        ctx = run.trigger_context if run is not None else None
        from_run = _text_id(ctx.get("case_id")) if isinstance(ctx, dict) else None
        case_ids = [from_run] if from_run else []
    payload: Dict[str, Any] = {
        trail.ACTION: "approval.request",
        trail.ACTOR: row.created_by_principal_id or trail.SYSTEM,
        trail.OBJECT: row.action_id,
        "action_id": row.action_id,
        "action_type": row.action_type,
        "effect_class": effect_class,
    }
    if row.workflow_run_id:
        payload["run_id"] = row.workflow_run_id
    if case_ids:
        payload["case_id"] = case_ids[0]
    trail.append(session, payload)


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
    # An approval that names only alert ids (a retired responder's, or one from
    # the create_approval_action tool) has no case link but the alerts' own.
    for finding_id in _evidence_ids(action):
        found = (evidence_cases or {}).get(finding_id)
        if found:
            return [found]
    return []


def case_ids_for(action: PendingAction) -> List[str]:
    """One action's cases, resolved as ``needs_you`` resolves them."""
    lookups = _case_lookups([action])
    return _case_ids_for_action(action, *lookups[:3])


def on_case(actions: List[PendingAction], case_id: str) -> List[PendingAction]:
    """Of ``actions``, those on ``case_id``, resolved as ``needs_you`` resolves them."""
    lookups = _case_lookups(actions)
    return [a for a in actions if case_id in _case_ids_for_action(a, *lookups[:3])]


def _evidence_ids(action: PendingAction) -> List[str]:
    return [one for one in map(_text_id, action.evidence or []) if one]


def _case_lookups(
    actions: List[PendingAction],
    session: Any = None,
) -> tuple[
    Dict[str, Any], Dict[str, Optional[str]], Dict[str, str], set[str], Dict[str, str]
]:
    """What those actions can name: run trigger contexts, investigation case ids,
    the newest case of each stored alert in their evidence, those alerts, and the
    status of every case any of them names. Reads in ``session`` when given, so
    a caller's own uncommitted writes are seen."""
    run_ids = {action.workflow_run_id for action in actions if action.workflow_run_id}
    investigation_ids: set[str] = set()
    case_ids: set[str] = set()
    for action in actions:
        params = action.parameters if isinstance(action.parameters, dict) else {}
        investigation_id = _text_id(params.get("investigation_id"))
        if investigation_id:
            investigation_ids.add(investigation_id)
        case_ids.update(proposal_case_ids(params))
    evidence_ids = {one for action in actions for one in _evidence_ids(action)}
    runs: Dict[str, Any] = {}
    investigations: Dict[str, Optional[str]] = {}
    evidence_cases: Dict[str, str] = {}
    stored: set[str] = set()
    statuses: Dict[str, str] = {}
    if not run_ids and not investigation_ids and not evidence_ids and not case_ids:
        return runs, investigations, evidence_cases, stored, statuses
    scope = (
        nullcontext(session)
        if session is not None
        else get_db_manager().session_scope()
    )
    with scope as session:
        if run_ids:
            rows = session.query(WorkflowRun).filter(WorkflowRun.run_id.in_(run_ids))
            for row in rows:
                ctx = row.trigger_context
                runs[row.run_id] = ctx if isinstance(ctx, dict) else {}
                from_run = _text_id(runs[row.run_id].get("case_id"))
                if from_run:
                    case_ids.add(from_run)
        if investigation_ids:
            rows = session.query(Investigation).filter(
                Investigation.investigation_id.in_(investigation_ids)
            )
            for row in rows:
                investigations[row.investigation_id] = row.case_id
                if _text_id(row.case_id):
                    case_ids.add(_text_id(row.case_id))
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
            case_ids.update(evidence_cases.values())
        if case_ids:
            statuses = dict(
                session.query(Case.case_id, Case.status).filter(
                    Case.case_id.in_(case_ids)
                )
            )
    return runs, investigations, evidence_cases, stored, statuses


# ---- Restricted cases (#4359) ----------------------------------------------
# An action is on the cases case_ids_for resolves. One the caller can't read is
# a placeholder on every read and not found on a decision. A reader is told
# when no reader of its case can decide it: it waits for one.

RESTRICTED_TITLE = "An action on a restricted case"
WAITING_FOR_READER = (
    "Waiting: no one who can read its case can approve it. "
    "Add a reader who holds approval rights."
)
DECIDE_PERMISSION = "ai_decisions.approve"


def _restricted_cases(case_ids: set[str]) -> tuple[set[str], Dict[str, List[str]]]:
    """Of ``case_ids``: the ones the caller can't read, and each restricted
    case's readers: the named principals and the members of named teams."""
    if not case_ids:
        return set(), {}
    from core.auth.permissions import case_read_clause, case_restricted
    from core.storage.models import TeamMembership

    with get_db_manager().session_scope() as session:
        hidden = {
            cid
            for (cid,) in session.query(Case.case_id).filter(
                Case.case_id.in_(case_ids), ~case_read_clause()
            )
        }
        rows = [
            row
            for row in session.query(
                Case.case_id, Case.reader_principal_ids, Case.reader_team_ids
            ).filter(Case.case_id.in_(case_ids - hidden))
            if case_restricted(row)
        ]
        team_ids = {team for row in rows for team in row.reader_team_ids or []}
        members: Dict[str, List[str]] = {}
        if team_ids:
            for team, principal in session.query(
                TeamMembership.team_id, TeamMembership.principal_id
            ).filter(TeamMembership.team_id.in_(team_ids)):
                members.setdefault(team, []).append(principal)
        readers = {
            row.case_id: list(row.reader_principal_ids or [])
            + [p for team in row.reader_team_ids or [] for p in members.get(team, [])]
            for row in rows
        }
    return hidden, readers


def _deciders(principal_ids: set[str]) -> set[str]:
    """Those principals that are active accounts holding the decide permission."""
    if not principal_ids:
        return set()
    from core.auth.permissions import missing_permission
    from core.storage.models import User

    with get_db_manager().session_scope() as session:
        return {
            principal
            for user_id, principal in session.query(
                User.user_id, User.principal_id
            ).filter(User.principal_id.in_(principal_ids), User.is_active.is_(True))
            if missing_permission(user_id, DECIDE_PERMISSION, session) is None
        }


def _placeholder(action: PendingAction) -> PendingAction:
    """What a non-reader sees: that an action exists, and its state."""
    return PendingAction(
        action_id=action.action_id,
        action_type="",
        title=RESTRICTED_TITLE,
        description="",
        target="",
        confidence=None,
        reason="",
        evidence=[],
        created_at=action.created_at,
        created_by="",
        requires_approval=action.requires_approval,
        status=action.status,
        approved_at=action.approved_at,
        parameters={},
        reversibility=action.reversibility,
        held=action.held,
        restricted=True,
    )


def screen_actions(
    actions: List[PendingAction],
    resolved: Optional[Dict[str, List[str]]] = None,
) -> List[PendingAction]:
    """``actions`` as the caller may see them; ``resolved`` is each action's
    cases when already worked out."""
    if resolved is None:
        lookups = _case_lookups(actions)
        resolved = {a.action_id: _case_ids_for_action(a, *lookups[:3]) for a in actions}
    hidden, readers = _restricted_cases(
        {cid for ids in resolved.values() for cid in ids}
    )
    pending = {
        a.action_id: a
        for a in actions
        if a.status == ActionStatus.PENDING.value
        and not hidden.intersection(resolved.get(a.action_id, []))
    }
    able = _deciders(
        {
            p
            for a in pending.values()
            for cid in resolved.get(a.action_id, [])
            for p in readers.get(cid, [])
        }
    )
    shown = []
    for action in actions:
        cases = resolved.get(action.action_id, [])
        if hidden.intersection(cases):
            shown.append(_placeholder(action))
            continue
        if action.action_id in pending and any(
            cid in readers and not able.intersection(readers[cid]) for cid in cases
        ):
            action.waiting = WAITING_FOR_READER
        shown.append(action)
    return shown


def screen_action(action: Optional[PendingAction]) -> Optional[PendingAction]:
    return None if action is None else screen_actions([action])[0]


def may_decide(action: PendingAction) -> bool:
    """Whether the caller may read every case the action is on."""
    hidden, _readers = _restricted_cases(set(case_ids_for(action)))
    return not hidden


def _refuse_unreadable(row: ApprovalActionRow) -> None:
    if not may_decide(_row_to_pending(row)):
        logger.warning("Action %s is on a restricted case: refused", row.action_id)
        raise ActionNotFound(row.action_id)


def _kind_for_action(action: PendingAction) -> str:
    params = action.parameters if isinstance(action.parameters, dict) else {}
    checkpoint = params.get("checkpoint_id")
    if isinstance(checkpoint, str):
        checkpoint = checkpoint.strip()
    if checkpoint:
        return "checkpoint"
    # Approving it runs a tool: it carries one, or a containment resolved to one.
    containment = params.get("containment")
    runs = runs_here(params) or (
        isinstance(containment, dict) and bool(containment.get("candidates"))
    )
    # Nothing executes it once approved, so it is advice, not a decision.
    if (
        not action.workflow_run_id
        and not runs
        and action.action_type not in ROLLOUT_ACTION_TYPES
    ):
        return "recommendation"
    return "approval"


def _checkpoint_fields(action: PendingAction) -> Dict[str, Any]:
    """What a checkpoint's decision block shows beyond its title.

    The checkpoint id ties the item to the run's own open checkpoint, so a page
    reading both can tell the question it is showing has been answered. For a
    hypothesis approval, the explanations being approved, each with where it came
    from: the benign one is the claim the others are tested against.
    """
    params = action.parameters if isinstance(action.parameters, dict) else {}
    checkpoint = params.get("checkpoint_id")
    tool, args = params.get("tool"), params.get("args")
    fields: Dict[str, Any] = {
        "checkpoint_id": checkpoint if isinstance(checkpoint, str) else None,
        "checkpoint_class": params.get("checkpoint_class") or None,
        "hypotheses": [],
        # A gated call's tool and the exact arguments approving it releases: the
        # invoke door runs only these, so the card must show them.
        "tool": tool if isinstance(tool, str) and tool else None,
        "args": args if isinstance(args, str) else None,
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


def _restricted_item(action: PendingAction, kind: str) -> Dict[str, Any]:
    return {
        "kind": kind,
        "source_id": action.action_id,
        "title": RESTRICTED_TITLE,
        "reason": "",
        "created_at": action.created_at,
        "reversibility": action.reversibility,
        "case_id": None,
        "case_ids": [],
        "finding_id": None,
        "run_id": None,
        "checkpoint_id": None,
        "checkpoint_class": None,
        "hypotheses": [],
        "tool": None,
        "args": None,
        "restricted": True,
    }


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
    runs, investigations, evidence_cases, stored, statuses = _case_lookups(actions)
    wanted = _text_id(case_id)
    by_action = {
        a.action_id: _case_ids_for_action(a, runs, investigations, evidence_cases)
        for a in actions
    }
    items: List[Dict[str, Any]] = []
    for original, action in zip(actions, screen_actions(list(actions), by_action)):
        resolved = by_action[action.action_id]
        if wanted is not None and wanted not in resolved:
            continue
        kind = _kind_for_action(original)
        # Closing its cases answers a recommendation; reopening one asks again.
        if (
            kind == "recommendation"
            and resolved
            and all(statuses.get(one) == CLOSED for one in resolved)
        ):
            continue
        if action.restricted:
            # Asked by case, a case the caller can't read has nothing to show.
            if wanted is None:
                items.append(_restricted_item(action, kind))
            continue
        items.append(
            {
                "kind": kind,
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
                "waiting": action.waiting,
                "containment": (action.parameters or {}).get("containment"),
                # The score the person deciding sees (#5278); None when unscored.
                "step_score": _decider_score(action.parameters),
                **_checkpoint_fields(action),
            }
        )
    pending_runs = {action.workflow_run_id for action in actions}
    parked_runs = _parked_runs({run for run in pending_runs if run})
    hidden, _readers = _restricted_cases(
        {p["case_id"] for p in parked_runs if p["case_id"]}
    )
    for parked in parked_runs:
        if wanted is not None and parked["case_id"] != wanted:
            continue
        # A run on a case the caller can't read is not theirs to keep going.
        if parked["case_id"] in hidden:
            continue
        items.append(parked)
    return {"count": len(items), "items": items}


def needs_you_case_ids() -> set[str]:
    """Case ids that need a person, from one ``needs_you()`` read."""
    return {one for item in needs_you()["items"] for one in item["case_ids"]}


# ---- A close settles its unanswered asks (#5107) ---------------------------
# Written in the close's own transaction, so every reader agrees with Home.

SETTLED_BY = "system"
SETTLED_PREFIX = "settled:"
_LIVE = (
    ActionStatus.PENDING.value,
    ActionStatus.APPROVED.value,
    ActionStatus.EXECUTED.value,
)


def _not_the_cases_ask(row: ApprovalActionRow) -> bool:
    """A run's question (its run resumes on the answer) or a rollout: no close
    answers these."""
    params = row.parameters if isinstance(row.parameters, dict) else {}
    checkpoint = params.get("checkpoint_id")
    return (
        bool(row.workflow_run_id)
        or (isinstance(checkpoint, str) and bool(checkpoint.strip()))
        or row.action_type in ROLLOUT_ACTION_TYPES
    )


def _lock_row(session, action_id: str) -> Optional[ApprovalActionRow]:
    """The row as committed, locked; None when a decision holds it. Skipped, not
    waited on: the caller already holds the trail lock, which a decision takes
    after its row, so waiting would deadlock. Two closers are already in turn on
    that trail lock."""
    return session.execute(
        select(ApprovalActionRow)
        .where(ApprovalActionRow.action_id == action_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _locked(session, action_ids: Iterable[str]) -> List[ApprovalActionRow]:
    """Lock rows one by one in ascending id order, so closers never cross."""
    rows = (_lock_row(session, one) for one in sorted(set(action_ids)))
    return [row for row in rows if row is not None]


def settle_for_close(session, case_id: str) -> int:
    """Reject, as the system, each pending ask naming ``case_id`` whose every
    named case is now closed. In the caller's transaction; returns how many."""
    candidates = [
        _row_to_pending(row)
        for row in session.execute(
            select(ApprovalActionRow)
            .where(ApprovalActionRow.status == ActionStatus.PENDING.value)
            .where(ApprovalActionRow.requires_approval.is_(True))
            .where(ApprovalActionRow.workflow_run_id.is_(None))
            .where(ApprovalActionRow.action_type.notin_(ROLLOUT_ACTION_TYPES))
        ).scalars()
    ]
    # Which cases a row names does not change with a close: resolve before locking.
    lookups = _case_lookups(candidates, session)[:3]
    ids = [
        a.action_id for a in candidates if case_id in _case_ids_for_action(a, *lookups)
    ]
    resolved: Dict[str, List[str]] = {}
    for row in _locked(session, ids):
        if row.status != ActionStatus.PENDING.value or _not_the_cases_ask(row):
            continue
        # Re-read from the locked row: a join may have listed another case.
        cases = _case_ids_for_action(_row_to_pending(row), *lookups)
        if case_id in cases:
            resolved[row.action_id] = cases
    if not resolved:
        return 0
    # Statuses after the locks: a peer that closed while this waited reads closed.
    named = {one for cases in resolved.values() for one in cases}
    statuses = dict(
        session.query(Case.case_id, Case.status).filter(Case.case_id.in_(named))
    )
    settled = 0
    for action_id, cases in resolved.items():
        if not all(statuses.get(one) == CLOSED for one in cases):
            continue
        row = session.get(ApprovalActionRow, action_id)
        kind = _kind_for_action(_row_to_pending(row))
        reason = (
            f"{SETTLED_PREFIX} {cases[0]} closed with this unanswered"
            if len(cases) == 1
            else f"{SETTLED_PREFIX} every case it named closed"
        )
        row.status = ActionStatus.REJECTED.value
        row.rejection_reason = reason
        row.approved_by = SETTLED_BY
        row.approved_by_principal_id = SETTLED_BY
        row.approved_at = utcnow()
        # A new dict: an in-place change to a JSON column is not flushed.
        row.parameters = {**(row.parameters or {}), "settled_cases": cases}
        session.flush()
        for one in cases:
            case_trail.append_approval(
                session,
                action=case_trail.SETTLE,
                principal_id=SETTLED_BY,
                action_id=action_id,
                username=SETTLED_BY,
                operator_text=reason,
                case_id=one,
                action_type=row.action_type,
                kind=kind,
            )
        settled += 1
    if settled:
        logger.info("Closing %s settled %d unanswered asks", case_id, settled)
    return settled


def _join_case(session, row: ApprovalActionRow, case_id: str) -> None:
    """List ``case_id`` on a pending row, as ``add_proposal_case`` does."""
    params = dict(row.parameters or {})
    named = proposal_case_ids(params)
    pending = _row_to_pending(row)
    listed = named or _case_ids_for_action(
        pending, *_case_lookups([pending], session)[:3]
    )
    if case_id in listed and named:
        return
    params.pop("case_id", None)
    params["case_ids"] = list(dict.fromkeys([*listed, case_id]))
    row.parameters = params


def reopen_settled(session, case_id: str) -> int:
    """Return to pending what closing ``case_id`` settled, unless a live ask for
    the same action and target covers it or it is past the expiry window. In the
    caller's transaction; returns how many."""
    from core.config import get_settings
    from core.response.proposal_targets import undone

    ids = session.execute(
        select(ApprovalActionRow.action_id)
        .where(ApprovalActionRow.status == ActionStatus.REJECTED.value)
        .where(ApprovalActionRow.approved_by == SETTLED_BY)
        .where(ApprovalActionRow.rejection_reason.like(f"{SETTLED_PREFIX}%"))
        .where(ApprovalActionRow.parameters.contains({"settled_cases": [case_id]}))
    ).scalars()
    cutoff = utcnow() - timedelta(days=get_settings().daemon_approval_expiry_days)
    restored = 0
    for row in _locked(session, ids):
        if row.status != ActionStatus.REJECTED.value:
            continue
        # The sweep would expire it on its next tick.
        if row.created_at < cutoff:
            continue
        # The newest live ask, passing over an executed act undone since.
        live = next(
            (
                one
                for one in session.execute(
                    _same_ask(row.action_type, row.target)
                    .where(ApprovalActionRow.created_at > row.created_at)
                    .where(ApprovalActionRow.status.in_(_LIVE))
                    .order_by(ApprovalActionRow.created_at.desc())
                ).scalars()
                if one.status != ActionStatus.EXECUTED.value
                or not undone(one.action_type, one.execution_result)
            ),
            None,
        )
        if live is not None:
            # A decided row takes no case; a pending one keeps this case asked.
            if live.status == ActionStatus.PENDING.value:
                live = _lock_row(session, live.action_id)
                if live is not None and live.status == ActionStatus.PENDING.value:
                    _join_case(session, live, case_id)
            continue
        params = dict(row.parameters or {})
        params.pop("settled_cases", None)
        row.parameters = params
        row.status = ActionStatus.PENDING.value
        row.rejection_reason = None
        row.approved_by = None
        row.approved_by_principal_id = None
        row.approved_at = None
        restored += 1
    session.flush()
    if restored:
        logger.info("Reopening %s asked %d settled asks again", case_id, restored)
    return restored


def reparent_for_merge(session, source_case_id: str, target_case_id: str) -> int:
    """Move pending asks naming the merged-away case onto the case that survives.
    In the caller's transaction; returns how many."""
    params = ApprovalActionRow.parameters
    ids = session.execute(
        select(ApprovalActionRow.action_id)
        .where(ApprovalActionRow.status == ActionStatus.PENDING.value)
        .where(
            or_(
                params.contains({"case_ids": [source_case_id]}),
                params.contains({"case_id": source_case_id}),
            )
        )
    ).scalars()
    moved = 0
    for row in _locked(session, ids):
        if row.status != ActionStatus.PENDING.value or _not_the_cases_ask(row):
            continue
        listed = proposal_case_ids(row.parameters)
        if source_case_id not in listed:
            continue
        cases = [target_case_id if one == source_case_id else one for one in listed]
        new = dict(row.parameters or {})
        new.pop("case_id", None)
        new["case_ids"] = list(dict.fromkeys(cases))
        row.parameters = new
        moved += 1
    session.flush()
    return moved
