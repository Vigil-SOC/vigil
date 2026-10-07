# What a decision does to a paused run. The answer is already recorded on the
# approval action; this only tells the agent layer there is one to read.

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from sqlalchemy import text

from core.agents.queue import build_resume_job, enqueue_run
from core.storage.connection import get_db_session

logger = logging.getLogger(__name__)


# Off the run event the worker journaled, never assumed: a hunt resumed as a
# compose is handed to the wrong workflow, which then finds nothing it recognises.
#
# Public because it is not only the resume path that needs it. Anything asking what
# kind a run is has to read it here: the first event carries the kind the worker
# opened the run as, which is the same value that worker decides everything else
# from. A run's workflow row can say something different -- a run started from file
# paths is named for its loop, not its definition -- so answering from that row
# instead is how two parts of one system come to disagree about one run.
def run_kind_of(run_id: str) -> Optional[str]:
    try:
        uuid.UUID(run_id)
    except ValueError:
        return None

    with get_db_session() as session:
        row = session.execute(
            text(
                "SELECT run_kind FROM agent_events "
                "WHERE run_id = CAST(:run_id AS uuid) ORDER BY seq LIMIT 1"
            ),
            {"run_id": run_id},
        ).one_or_none()
    return None if row is None else str(row.run_kind)


# Asks the agent layer to pick the run back up after a decision.
async def resume_run(run_id: str, action_id: str, decided_by: str) -> Dict[str, Any]:
    run_kind = run_kind_of(run_id)
    if run_kind is None:
        # Nothing on the ledger to resume. The decision is still recorded; there
        # is simply no agent-layer run behind it, which a compose-era approval is.
        logger.info("no agent-layer ledger for run %s; nothing to resume", run_id)
        return {"success": False, "run_id": run_id, "error": "no ledger for this run"}

    job = build_resume_job(run_id=run_id, run_kind=run_kind, enqueued_by=decided_by)
    try:
        # No job id of our own: a failed resume would keep it and wedge the run on
        # the approval it waits for. agent_run_leases refuses the second worker.
        job_id = await enqueue_run(job)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "could not resume run %s after decision %s: %s", run_id, action_id, exc
        )
        return {"success": False, "run_id": run_id, "error": "run queue unavailable"}

    return {"success": True, "status": "resuming", "run_id": run_id, "job_id": job_id}
