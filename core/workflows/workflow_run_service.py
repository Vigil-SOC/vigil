from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from core.storage.connection import get_db_manager
from core.storage.models import WorkflowRun, WorkflowRunPhase
from core.storage.schemas import WorkflowRunPhaseSchema, WorkflowRunSchema
from core.telemetry import get_meter
from core.time import utcnow

logger = logging.getLogger(__name__)

# Same ceiling as GET /workflows/{workflow_id}/runs.
LIST_RUNS_MAX = 200

_runs_finished: Any = None


def _runs_finished_counter() -> Any:
    """Created on first use: ``get_meter`` before ``init_telemetry`` is a
    permanent no-op, and this module is imported at API boot."""
    global _runs_finished
    if _runs_finished is None:
        _runs_finished = get_meter("vigil.workflows.runs").create_counter(
            "vigil.runs.finished",
            description="Workflow runs reaching a terminal status, by run_kind",
            unit="1",
        )
    return _runs_finished


def generate_run_id() -> str:
    """Return a new run_id shaped ``wfr-YYYYMMDD-<uuid8>``."""
    return f"wfr-{utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"


def _as_naive_utc(value: datetime) -> datetime:
    """Match workflow_runs columns, which store naive UTC."""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


class WorkflowRunService:
    """Persist and query workflow execution history."""

    def begin_run(
        self,
        *,
        workflow_id: str,
        workflow_name: str,
        workflow_source: str = "file",
        workflow_version: Optional[int] = None,
        trigger_context: Optional[Dict[str, Any]] = None,
        triggered_by: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> Optional[str]:
        """Create a ``workflow_runs`` row with ``status='running'``.

        Returns the new ``run_id`` on success, ``None`` if the DB
        write fails (the workflow still executes — run history is
        best-effort so a DB outage can't block operations).

        A caller may supply ``run_id`` when the run is already
        identified elsewhere, so one run carries one id everywhere.
        """
        run_id = run_id or generate_run_id()
        try:
            db = get_db_manager()
            with db.session_scope() as session:
                row = WorkflowRun(
                    run_id=run_id,
                    workflow_id=workflow_id,
                    workflow_version=workflow_version,
                    workflow_source=workflow_source,
                    workflow_name=workflow_name,
                    status="running",
                    triggered_by=triggered_by,
                    trigger_context=trigger_context or {},
                    started_at=utcnow(),
                )
                session.add(row)
                session.flush()
            logger.info("Workflow run started: %s (workflow=%s)", run_id, workflow_id)
            return run_id
        except SQLAlchemyError as e:
            logger.warning("Could not persist workflow run start: %s", e)
            return None

    def set_status(self, run_id: str, status: str) -> bool:
        """Update only ``workflow_runs.status`` without touching terminal
        fields. Used by the phase loop to flip running→paused when a
        phase blocks on approval (#128)."""
        if status not in ("running", "paused"):
            logger.error("set_status: invalid non-terminal status %r", status)
            return False
        try:
            db = get_db_manager()
            with db.session_scope() as session:
                row = session.get(WorkflowRun, run_id)
                if row is None:
                    return False
                row.status = status
            return True
        except SQLAlchemyError as e:
            logger.warning("Could not set run status %s: %s", run_id, e)
            return False

    def finalize_run(
        self,
        run_id: str,
        *,
        status: str,
        result_summary: Optional[str] = None,
        error: Optional[str] = None,
        cost_usd: Optional[float] = None,
    ) -> bool:
        """Mark a run terminal. ``status`` must be one of the check-
        constrained values: completed | failed | cancelled."""
        if status not in ("completed", "failed", "cancelled"):
            logger.error("finalize_run: invalid status %r", status)
            return False
        try:
            db = get_db_manager()
            with db.session_scope() as session:
                row = session.get(WorkflowRun, run_id)
                if row is None:
                    logger.warning("finalize_run: unknown run %s", run_id)
                    return False
                # Only runs begun via the agent-runs or workflows start routes
                # carry it; anything else is labelled rather than dropped.
                run_kind = (row.trigger_context or {}).get("run_kind") or "unknown"
                now = utcnow()
                row.status = status
                row.finished_at = now
                # Truncate result_summary to avoid committing megabyte
                # prompt transcripts to the DB — full transcripts live
                # in the reasoning_traces table.
                if result_summary is not None:
                    row.result_summary = result_summary[:50_000]
                if error is not None:
                    row.error = str(error)[:5_000]
                if cost_usd is not None:
                    row.total_cost_usd = cost_usd
                if row.started_at is not None:
                    delta = now - row.started_at
                    row.duration_ms = int(delta.total_seconds() * 1000)
        except SQLAlchemyError as e:
            logger.warning("Could not finalise workflow run %s: %s", run_id, e)
            return False
        # After the commit, so a write that fails is not counted as an outcome.
        _runs_finished_counter().add(1, {"run_kind": str(run_kind), "status": status})
        logger.info("Workflow run finalised: %s -> %s", run_id, status)
        return True

    def list_runs(
        self,
        *,
        workflow_id: Optional[str] = None,
        workflow_source: Optional[str] = None,
        status: Optional[str] = None,
        run_kind: Optional[str] = None,
        started_at: Optional[datetime] = None,
        finished_at: Optional[datetime] = None,
        finished_after: Optional[datetime] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List runs, newest first. Does not include the (potentially
        large) ``result_summary`` field — use ``get_run`` for detail.

        ``workflow_source`` filters by how the run was started (e.g. "agent"
        for runs enqueued through the agent-runs API). ``run_kind`` matches
        the ``run_kind`` key of ``trigger_context``. ``started_at`` is an
        inclusive lower bound on when the run started. ``finished_after`` /
        ``finished_at`` bound when it finished (inclusive).
        """
        try:
            db = get_db_manager()
            with db.session_scope() as session:
                # Deleted rows are hidden here rather than dropped from the table:
                # the ledger behind a run is the only account of what an agent did.
                stmt = select(WorkflowRun).where(WorkflowRun.deleted_at.is_(None))
                if workflow_id:
                    stmt = stmt.where(WorkflowRun.workflow_id == workflow_id)
                if workflow_source:
                    stmt = stmt.where(WorkflowRun.workflow_source == workflow_source)
                if status:
                    stmt = stmt.where(WorkflowRun.status == status)
                if run_kind:
                    stmt = stmt.where(
                        WorkflowRun.trigger_context["run_kind"].astext == run_kind
                    )
                if started_at is not None:
                    stmt = stmt.where(
                        WorkflowRun.started_at >= _as_naive_utc(started_at)
                    )
                if finished_after is not None:
                    stmt = stmt.where(
                        WorkflowRun.finished_at >= _as_naive_utc(finished_after)
                    )
                if finished_at is not None:
                    stmt = stmt.where(
                        WorkflowRun.finished_at <= _as_naive_utc(finished_at)
                    )
                stmt = (
                    stmt.order_by(WorkflowRun.started_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
                rows = session.execute(stmt).scalars().all()
                return [WorkflowRunSchema.dump_summary(r) for r in rows]
        except SQLAlchemyError as e:
            logger.warning("Error listing workflow runs: %s", e)
            return []

    # Raises rather than swallowing, unlike the listings around it. Those serve
    # views, where an empty answer is a fair degraded one. A caller asking whether
    # a trigger has already produced a run needs "could not tell" to be a different
    # answer from "no" -- returning None on a failed query is the one that has it
    # act as though nothing had run.
    def find_run_by_trigger(self, triggered_by: str) -> Optional[Dict[str, Any]]:
        """The newest live run a ``triggered_by`` key produced, or None for none.

        Exact, rather than scanning the most recent runs of a workflow: the window
        such a scan would have to span is however long whatever fires the run takes
        to come back, which is not a number this side can pick.
        """
        db = get_db_manager()
        with db.session_scope() as session:
            stmt = (
                select(WorkflowRun)
                .where(
                    WorkflowRun.triggered_by == triggered_by,
                    WorkflowRun.deleted_at.is_(None),
                )
                .order_by(WorkflowRun.started_at.desc())
                .limit(1)
            )
            row = session.execute(stmt).scalars().first()
            return WorkflowRunSchema.dump_summary(row) if row else None

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Get one run with the full ``result_summary`` attached."""
        try:
            db = get_db_manager()
            with db.session_scope() as session:
                row = session.get(WorkflowRun, run_id)
                return WorkflowRunSchema.dump(row) if row else None
        except SQLAlchemyError as e:
            logger.warning("Error fetching workflow run %s: %s", run_id, e)
            return None

    def delete_run(self, run_id: str) -> bool:
        """Hide ``run_id`` from the listings. False when there is no such live run."""
        try:
            db = get_db_manager()
            with db.session_scope() as session:
                row = session.get(WorkflowRun, run_id)
                if row is None or row.deleted_at is not None:
                    return False
                row.deleted_at = utcnow()
                return True
        except SQLAlchemyError as e:
            logger.warning("Error deleting workflow run %s: %s", run_id, e)
            return False

    def set_result_summary(self, run_id: str, summary: str) -> bool:
        """Re-render the stored account of a run that has already finished.

        Apart from ``finalize_run`` because the run is not ending again: its
        status, its cost and when it stopped all stand, and only the write-up
        is being replaced. Truncated on the same ceiling.
        """
        try:
            db = get_db_manager()
            with db.session_scope() as session:
                row = session.get(WorkflowRun, run_id)
                if row is None:
                    return False
                row.result_summary = summary[:50_000]
                return True
        except SQLAlchemyError as e:
            logger.warning("Could not restate the summary of %s: %s", run_id, e)
            return False

    # ------------------------------------------------------------------
    # Phase-level helpers (#128)
    # ------------------------------------------------------------------

    def upsert_phase(
        self,
        run_id: str,
        phase_id: str,
        *,
        phase_order: int,
        agent_id: str,
        status: str,
        input_context: Optional[Dict[str, Any]] = None,
        output: Optional[Dict[str, Any]] = None,
        approval_state: Optional[str] = None,
        error: Optional[str] = None,
        started_at: Optional[datetime] = None,
        finished_at: Optional[datetime] = None,
    ) -> bool:
        """Insert or update a ``workflow_run_phases`` row.

        The phase loop in ``WorkflowsService.execute_workflow`` calls
        this at each state transition (pending → running → completed
        / failed / pending_approval). ``upsert`` semantics keep the
        call sites simple — they don't need to know whether a prior
        row exists on retry/resume.
        """
        try:
            db = get_db_manager()
            with db.session_scope() as session:
                row = session.get(WorkflowRunPhase, (run_id, phase_id))
                if row is None:
                    row = WorkflowRunPhase(
                        run_id=run_id,
                        phase_id=phase_id,
                        phase_order=phase_order,
                        agent_id=agent_id,
                        status=status,
                        input_context=dict(input_context or {}),
                        output=dict(output or {}),
                        approval_state=approval_state,
                        error=error,
                        started_at=started_at,
                        finished_at=finished_at,
                    )
                    session.add(row)
                else:
                    row.phase_order = phase_order
                    row.agent_id = agent_id
                    row.status = status
                    if input_context is not None:
                        row.input_context = dict(input_context)
                    if output is not None:
                        row.output = dict(output)
                    if approval_state is not None:
                        row.approval_state = approval_state
                    if error is not None:
                        row.error = error
                    if started_at is not None:
                        row.started_at = started_at
                    if finished_at is not None:
                        row.finished_at = finished_at
                        if row.started_at:
                            delta = finished_at - row.started_at
                            row.duration_ms = int(delta.total_seconds() * 1000)
            return True
        except SQLAlchemyError as e:
            logger.warning(
                "Could not upsert phase %s/%s: %s",
                run_id,
                phase_id,
                e,
            )
            return False

    def list_phases(self, run_id: str) -> List[Dict[str, Any]]:
        """Return all phase rows for a run, ordered by ``phase_order``."""
        try:
            db = get_db_manager()
            with db.session_scope() as session:
                stmt = (
                    select(WorkflowRunPhase)
                    .where(WorkflowRunPhase.run_id == run_id)
                    .order_by(WorkflowRunPhase.phase_order)
                )
                rows = session.execute(stmt).scalars().all()
                return WorkflowRunPhaseSchema.dump_many(rows)
        except SQLAlchemyError as e:
            logger.warning("Error listing phases for run %s: %s", run_id, e)
            return []
