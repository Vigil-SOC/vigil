"""End agent runs whose start job is gone.

A run's ``workflow_runs`` row is written ``running`` before its start job is
enqueued. A job that is lost before a worker claims it leaves the run live on its
case for good: it has no lease row for the worker's sweeper to find and no ledger
events. The enqueue-failure paths end such a run as failed; this does the same
for one whose job vanished after a successful enqueue.

A run is lost only when all of these hold: it is a running run that was started
through the agent queue (its trigger context carries ``run_kind``, which every
caller that enqueues a start job sets, whatever its ``workflow_source``; a paused
run always has a lease), it has no lease row and no ledger events, its job id (the run
id) is not waiting, delayed, active or otherwise held in the queue, and its row is
older than ``START_GRACE_SECONDS``. A run that is merely queued while no worker
runs is left alone and continues when a worker returns. A run whose worker finished
and whose terminal report was lost is not covered: it has events.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable, List, Optional, Tuple

from sqlalchemy import text

from core.agents.attribution import note_terminal
from core.agents.projections import run_id_for
from core.agents.queue import _run_queue, close_run_queue
from core.storage.connection import get_db_manager
from core.workflows.workflow_run_service import (
    DURATION_MS_MAX,
    _runs_finished_counter,
)

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
    "r.trigger_context->>'run_kind' IS NOT NULL AND r.status = 'running' "
    "AND r.run_id ~* :uuid "
    f"AND NOT EXISTS (SELECT 1 FROM agent_run_leases l WHERE l.run_id = {_RUN_UUID}) "
    f"AND NOT EXISTS (SELECT 1 FROM agent_events e WHERE e.run_id = {_RUN_UUID})"
)


PAGE = 100
# Pages per pass. The caller keeps the cursor between passes, so a long backlog of
# merely queued runs is walked across ticks rather than held in one.
MAX_PAGES = 10

Cursor = Tuple[datetime, str]


def _candidates(after: Optional[Cursor]) -> List[Cursor]:
    """The next page of unstarted runs, oldest first, past ``after``. A keyset, so
    runs that are queued and kept do not crowd out a lost one behind them."""
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            text(
                f"SELECT r.started_at, r.run_id FROM workflow_runs r WHERE {_UNSTARTED} "
                "AND r.started_at < timezone('utc', now()) "
                "    - make_interval(secs => :grace) "
                "AND (CAST(:after_at AS timestamp) IS NULL "
                "     OR (r.started_at, r.run_id) > (:after_at, :after_id)) "
                "ORDER BY r.started_at, r.run_id LIMIT :limit"
            ),
            {
                "uuid": _UUID,
                "grace": START_GRACE_SECONDS,
                "after_at": after[0] if after else None,
                "after_id": after[1] if after else None,
                "limit": PAGE,
            },
        ).all()
        return [(r[0], str(r[1])) for r in rows]


def _claim(run_id: str) -> Optional[Tuple[str, Optional[str]]]:
    """End the run as failed, whole, if it is still unstarted: the status and the
    fields ``finalize_run`` would set go in one write, so the row is never left
    half-ended. One caller wins: the write re-reads every condition, so two daemons
    end a run once. Returns its run_kind, or None when the run was not won."""
    with get_db_manager().session_scope() as session:
        won = session.execute(
            text(
                "UPDATE workflow_runs r SET status = 'failed', error = :reason, "
                "  finished_at = timezone('utc', now()), "
                "  duration_ms = CASE "
                "    WHEN extract(epoch FROM timezone('utc', now()) - r.started_at) "
                "         * 1000 > :int4_max THEN NULL "
                "    ELSE (extract(epoch FROM timezone('utc', now()) "
                "          - r.started_at) * 1000)::int END "
                f"WHERE r.run_id = :run_id AND {_UNSTARTED} "
                "RETURNING coalesce(r.trigger_context->>'run_kind', 'unknown'), "
                "          r.trigger_context->>'investigation_id'"
            ),
            {
                "uuid": _UUID,
                "run_id": run_id,
                "reason": REASON,
                "int4_max": DURATION_MS_MAX,
            },
        ).first()
    return None if won is None else (won[0], won[1])


async def end_lost_start_jobs(
    after: Optional[Cursor], on_investigation: Callable[[str], None]
) -> Optional[Cursor]:
    """Fail each run whose start job is gone, from ``after`` on; returns where the
    next pass resumes, or None once the end is reached (the next pass starts over).

    ``on_investigation`` is called with the investigation whose own run was just
    ended, for the caller to fail as the enqueue-failure path does; it must not
    raise. A queue that cannot be read ends the pass: a job it cannot see is not a
    job that is gone. One run failing to end does not stop the rest.
    """
    for _ in range(MAX_PAGES):
        page = await asyncio.to_thread(_candidates, after)
        for started_at, run_id in page:
            try:
                state = await asyncio.wait_for(
                    _run_queue().getJobState(run_id), QUEUE_READ_TIMEOUT_SECONDS
                )
            except Exception as exc:  # noqa: BLE001 - Redis down is not a lost job
                logger.warning("start-job check skipped, queue unreadable: %s", exc)
                try:
                    await asyncio.wait_for(
                        close_run_queue(), QUEUE_READ_TIMEOUT_SECONDS
                    )
                except Exception:  # noqa: BLE001
                    pass
                return after
            after = (started_at, run_id)
            if state not in LOST_STATES:
                continue
            try:
                won = await asyncio.to_thread(_claim, run_id)
                if won is None:
                    continue
                run_kind, investigation_id = won
                # A shadow run shares its investigation's id in its context; only
                # the investigation's own run decides the investigation. First, so
                # nothing after can drop it.
                if investigation_id and run_id == run_id_for(investigation_id):
                    on_investigation(investigation_id)
                # What the enqueue-failure paths do, less the row, written above.
                _runs_finished_counter().add(
                    1, {"run_kind": run_kind, "status": "failed"}
                )
                await asyncio.to_thread(note_terminal, run_id, "failed", "")
                logger.warning(
                    "ended run %s: its start job was lost (%s)", run_id, state
                )
            except Exception:  # noqa: BLE001 - one run must not stop the rest
                logger.exception("could not end lost run %s", run_id)
        if len(page) < PAGE:
            return None
    return after
