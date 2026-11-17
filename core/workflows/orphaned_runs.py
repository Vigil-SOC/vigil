"""End agent runs that nothing will ever finish.

A live worker's lease sweeper ends a stalled run, but only if a worker is running
and the run has a lease row. A run whose worker is gone, or whose start job was
lost before it claimed, stays ``running`` and reads as live on its case for good.
This runs on the daemon, which does not depend on the worker.

A run is left alone while it could still be held: an unexpired lease, a lapse
shorter than ``LAPSED_GRACE`` (the sweeper acts within seconds of one), or a
start younger than ``UNCLAIMED_GRACE`` (a queued job may wait behind a busy
worker). A parked run is ``paused`` with a lease that runs to its park deadline,
so waiting on a person is never mistaken for being orphaned: it ends only once
that deadline is long past with no worker to act on it.
"""

from __future__ import annotations

import logging
from typing import List, Tuple

from sqlalchemy import text

from core.storage.connection import get_db_manager
from core.workflows.workflow_run_service import (
    IN_FLIGHT_STATUSES,
    WorkflowRunService,
)

logger = logging.getLogger(__name__)

# How long past its deadline a lease may sit before no worker is assumed.
LAPSED_GRACE_SECONDS = 3600
# How long a run may wait for its start job to claim before it is assumed lost.
UNCLAIMED_GRACE_SECONDS = 6 * 3600

REASON = "No agent worker held this run; it was ended rather than left running"

# Console ids that are not uuids have no lease to compare, and are not agent runs.
_UUID = "^[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}$"


def _orphans() -> List[Tuple[str, str]]:
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            text(
                "SELECT r.run_id, r.trigger_context->>'case_id' "
                "FROM workflow_runs r "
                "LEFT JOIN agent_run_leases l "
                "  ON r.run_id ~* :uuid AND l.run_id = CAST(r.run_id AS uuid) "
                "WHERE r.workflow_source = 'agent' "
                "  AND r.status = ANY(:live) "
                "  AND ((l.run_id IS NULL "
                "        AND r.started_at < timezone('utc', now()) "
                "            - make_interval(secs => :unclaimed)) "
                "       OR l.claim_until < now() - make_interval(secs => :lapsed))"
            ),
            {
                "uuid": _UUID,
                "live": list(IN_FLIGHT_STATUSES),
                "unclaimed": UNCLAIMED_GRACE_SECONDS,
                "lapsed": LAPSED_GRACE_SECONDS,
            },
        ).all()
    return [(r[0], r[1]) for r in rows]


def end_orphaned_runs() -> int:
    """Finalize each orphaned run as failed; returns how many were ended.

    Goes through the same finalize, approval withdrawal and ledger terminal as a
    run the worker ends itself, so a worker that returns finds it already over.
    """
    from core.response.checkpoints import withdraw_for_run
    from core.workflows.run_bridge_router import _add_activity
    from core.workflows.run_cancel import force_terminal

    runs = WorkflowRunService()
    ended = 0
    for run_id, case_id in _orphans():
        # Read again: the run may have ended since the list was taken.
        current = runs.get_run(run_id)
        if current is None or current.get("status") not in IN_FLIGHT_STATUSES:
            continue
        withdraw_for_run(run_id, REASON)
        force_terminal(run_id, REASON, outcome="failed")
        if not runs.finalize_run(
            run_id,
            status="failed",
            error=REASON,
            outcome="failed",
            reason=REASON,
        ):
            continue
        ended += 1
        logger.warning("ended orphaned run %s", run_id)
        if case_id:
            try:
                _add_activity(
                    case_id,
                    "agent_run_report",
                    REASON,
                    {"run_id": run_id, "outcome": "failed"},
                )
            except Exception:  # noqa: BLE001 — the run ended either way
                logger.exception("could not record %s on case %s", run_id, case_id)
    return ended
