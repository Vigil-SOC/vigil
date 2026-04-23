"""CRUD + lifecycle service for workflow run history (#127).

Before this service, workflows executed and vanished — there was no
way to answer "what was the last run of incident-response on case X?"
or "why did this workflow fail yesterday?". This module owns the
`workflow_runs` table lifecycle: insert a row at execute-start with
``status='running'``, update at execute-end with the final status,
result summary, error, and duration. Runs are surfaced through the
API by ``backend/api/workflows.py``.

Per-phase rows (``workflow_run_phases``) are reserved for phase-by-
phase execution (#128) and aren't written yet — the parent row alone
carries the run's audit story for the current "composite prompt" path.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from database.connection import get_db_manager
from database.models import WorkflowRun

logger = logging.getLogger(__name__)


def generate_run_id() -> str:
    """Return a new run_id shaped ``wfr-YYYYMMDD-<uuid8>``."""
    return f"wfr-{datetime.utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"


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
        skill_tools_available: Optional[List[str]] = None,
    ) -> Optional[str]:
        """Create a ``workflow_runs`` row with ``status='running'``.

        Returns the new ``run_id`` on success, ``None`` if the DB
        write fails (the workflow still executes — run history is
        best-effort so a DB outage can't block operations).
        """
        run_id = generate_run_id()
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
                    started_at=datetime.utcnow(),
                    skill_tools_available=list(skill_tools_available or []),
                )
                session.add(row)
                session.flush()
            logger.info("Workflow run started: %s (workflow=%s)", run_id, workflow_id)
            return run_id
        except SQLAlchemyError as e:
            logger.warning("Could not persist workflow run start: %s", e)
            return None

    def finalize_run(
        self,
        run_id: str,
        *,
        status: str,
        result_summary: Optional[str] = None,
        error: Optional[str] = None,
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
                now = datetime.utcnow()
                row.status = status
                row.finished_at = now
                # Truncate result_summary to avoid committing megabyte
                # prompt transcripts to the DB — full transcripts live
                # in the reasoning_traces table.
                if result_summary is not None:
                    row.result_summary = result_summary[:50_000]
                if error is not None:
                    row.error = str(error)[:5_000]
                if row.started_at is not None:
                    delta = now - row.started_at
                    row.duration_ms = int(delta.total_seconds() * 1000)
            logger.info("Workflow run finalised: %s -> %s", run_id, status)
            return True
        except SQLAlchemyError as e:
            logger.warning("Could not finalise workflow run %s: %s", run_id, e)
            return False

    def list_runs(
        self,
        *,
        workflow_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List runs, newest first. Does not include the (potentially
        large) ``result_summary`` field — use ``get_run`` for detail."""
        try:
            db = get_db_manager()
            with db.session_scope() as session:
                stmt = select(WorkflowRun)
                if workflow_id:
                    stmt = stmt.where(WorkflowRun.workflow_id == workflow_id)
                if status:
                    stmt = stmt.where(WorkflowRun.status == status)
                stmt = (
                    stmt.order_by(WorkflowRun.started_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
                rows = session.execute(stmt).scalars().all()
                return [r.to_dict(include_result=False) for r in rows]
        except SQLAlchemyError as e:
            logger.warning("Error listing workflow runs: %s", e)
            return []

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Get one run with the full ``result_summary`` attached."""
        try:
            db = get_db_manager()
            with db.session_scope() as session:
                row = session.get(WorkflowRun, run_id)
                return row.to_dict(include_result=True) if row else None
        except SQLAlchemyError as e:
            logger.warning("Error fetching workflow run %s: %s", run_id, e)
            return None


_service: Optional[WorkflowRunService] = None


def get_workflow_run_service() -> WorkflowRunService:
    """Process-wide singleton."""
    global _service
    if _service is None:
        _service = WorkflowRunService()
    return _service
