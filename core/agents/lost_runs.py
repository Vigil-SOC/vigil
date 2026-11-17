"""End agent runs whose start job is gone.

A run's ``workflow_runs`` row is written ``running`` before its start job is
enqueued. A job that is lost before a worker claims it leaves the run live on its
case for good: it has no lease row for the worker's sweeper to find and no ledger
events. The enqueue-failure paths end such a run as failed; this does the same
for one whose job vanished after a successful enqueue.

A run is lost only when all of these hold: it is a running agent run (a paused one
always has a lease), it has no lease row and no ledger events, its job id (the run
id) is not waiting, delayed, active or otherwise held in the queue, and its row is
older than ``START_GRACE_SECONDS``. A run that is merely queued while no worker
runs is left alone and continues when a worker returns. A run whose worker finished
and whose terminal report was lost is not covered: it has events.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, List, Sequence

from sqlalchemy import text

from core.agents.attribution import note_terminal
from core.agents.queue import _run_queue, close_run_queue
from core.storage.connection import get_db_manager
from core.workflows.workflow_run_service import WorkflowRunService

logger = logging.getLogger(__name__)

# The row is written, then the start job enqueued, in the same request or tick: a
# matter of seconds, and a failed enqueue is retried within the 5 s and 10 s
# backoffs. Ten minutes is far past both and short enough that a lost run does not
# read as live for long.
START_GRACE_SECONDS = 600

# What the queue says of a job it no longer holds: never added or since removed,
# finished, or failed with its attempts spent. Every other state is a job a worker
# can still take, or a queue that is paused.
LOST_STATES = frozenset({"unknown", "completed", "failed"})

# The queue client retries a lost connection without end, so a read against a Redis
# that is down would never return.
QUEUE_READ_TIMEOUT_SECONDS = 10

REASON = "Could not enqueue: the start job is no longer queued"

_UUID = "^[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}$"
# The cast is guarded: a console id that is not a uuid has no lease or ledger.
_RUN_UUID = "CASE WHEN r.run_id ~* :uuid THEN CAST(r.run_id AS uuid) END"
_UNSTARTED = (
    "r.workflow_source = 'agent' AND r.status = 'running' "
    "AND r.run_id ~* :uuid "
    f"AND NOT EXISTS (SELECT 1 FROM agent_run_leases l WHERE l.run_id = {_RUN_UUID}) "
    f"AND NOT EXISTS (SELECT 1 FROM agent_events e WHERE e.run_id = {_RUN_UUID})"
)


def _candidates(limit: int = 100) -> List[str]:
    with get_db_manager().session_scope() as session:
        rows: Sequence[Any] = (
            session.execute(
                text(
                    f"SELECT r.run_id FROM workflow_runs r WHERE {_UNSTARTED} "
                    "AND r.started_at < timezone('utc', now()) "
                    "    - make_interval(secs => :grace) "
                    "ORDER BY r.started_at LIMIT :limit"
                ),
                {"uuid": _UUID, "grace": START_GRACE_SECONDS, "limit": limit},
            )
            .scalars()
            .all()
        )
        return [str(r) for r in rows]


def _claim(run_id: str) -> bool:
    """Leave ``running`` if the run is still unstarted. One caller wins: the write
    re-reads every condition, so two daemons end a run once."""
    with get_db_manager().session_scope() as session:
        won = session.execute(
            text(
                f"UPDATE workflow_runs r SET status = 'failed' "
                f"WHERE r.run_id = :run_id AND {_UNSTARTED} RETURNING r.run_id"
            ),
            {"uuid": _UUID, "run_id": run_id},
        ).first()
    return won is not None


async def end_lost_start_jobs() -> int:
    """Fail each run whose start job is gone; returns how many were ended.

    A queue that cannot be read ends the pass: a job it cannot see is not a job
    that is gone.
    """
    candidates = _candidates()
    if not candidates:
        return 0
    runs = WorkflowRunService()
    ended = 0
    for run_id in candidates:
        try:
            state = await asyncio.wait_for(
                _run_queue().getJobState(run_id), QUEUE_READ_TIMEOUT_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 - Redis down is not a lost job
            logger.warning("start-job check skipped, queue unreadable: %s", exc)
            await close_run_queue()
            return ended
        if state not in LOST_STATES:
            continue
        try:
            if not _claim(run_id):
                continue
            # What the enqueue-failure paths do.
            runs.finalize_run(run_id, status="failed", error=REASON)
            note_terminal(run_id, "failed", "")
            ended += 1
            logger.warning("ended run %s: its start job was lost (%s)", run_id, state)
        except Exception:  # noqa: BLE001 - one run must not stop the rest
            logger.exception("could not end lost run %s", run_id)
    return ended
