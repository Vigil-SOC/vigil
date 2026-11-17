"""End agent runs that nothing will ever finish.

A live worker's lease sweeper ends a stalled run, but only if a worker is running
and the run has a lease row. A run whose worker is gone, or whose start job was
lost before it claimed, stays ``running`` and reads as live on its case for good.
This runs on the daemon, which does not depend on the worker.

A run is left alone while it could still be held: an unexpired lease, a lapse
shorter than ``LAPSED_GRACE_SECONDS``, or a start younger than
``UNCLAIMED_GRACE_SECONDS`` (a queued job may wait behind a busy worker). A parked
run is ``paused`` and keeps its lease row, which the worker re-arms every park
poll (a minute by default), so waiting on a person is never mistaken for being
orphaned: it ends only once its lease has gone unrenewed for the grace, which
means no worker has looked at it for that long. A worker outage longer than that
therefore also ends a paused run still waiting on a human.

A run whose ledger already holds a terminal is not failed: the worker finished it
and only the report to the console was lost, so the row takes that outcome.
A run ended here is sealed (``stop_cause``): a worker that later dequeues a
lost start job cannot overwrite it with its own terminal.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from core.storage.connection import get_db_manager
from core.workflows.workflow_run_service import (
    IN_FLIGHT_STATUSES,
    ORPHANED_STOP_CAUSE,
    WorkflowRunService,
)

logger = logging.getLogger(__name__)


def _ms_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# The agent layer's own intervals (core/leases.ts), so a deployment that raises
# them does not have live leases read as lapsed.
_LONGEST_INTERVAL_S = (
    max(
        _ms_env("VIGIL_PARK_POLL_MS", 60_000),
        _ms_env("VIGIL_DENIAL_RECHECK_MS", 900_000),
        _ms_env("VIGIL_SWEEP_MS", 30_000),
        _ms_env("VIGIL_LEASE_TTL_MS", 60_000),
    )
    // 1000
)

# How long past its deadline a lease may sit before no worker is assumed.
LAPSED_GRACE_SECONDS = max(3600, 4 * _LONGEST_INTERVAL_S)
# How long a run may wait for its start job to claim before it is assumed lost.
UNCLAIMED_GRACE_SECONDS = 6 * 3600

REASON = "No agent worker held this run; it was ended rather than left running"

# The run's rows that nobody holds. Matching on text keeps a console id that is
# not a uuid from failing the cast.
_ORPHANED = (
    "r.workflow_source = 'agent' "
    "AND r.status = ANY(:live) "
    "AND ((NOT EXISTS (SELECT 1 FROM agent_run_leases l "
    "                  WHERE l.run_id::text = r.run_id) "
    "      AND r.started_at < timezone('utc', now()) "
    "          - make_interval(secs => :unclaimed)) "
    "     OR EXISTS (SELECT 1 FROM agent_run_leases l "
    "                WHERE l.run_id::text = r.run_id "
    "                  AND l.claim_until < now() - make_interval(secs => :lapsed)))"
)


def _params() -> Dict[str, Any]:
    return {
        "live": list(IN_FLIGHT_STATUSES),
        "unclaimed": UNCLAIMED_GRACE_SECONDS,
        "lapsed": LAPSED_GRACE_SECONDS,
    }


def _orphans() -> List[Tuple[str, Optional[str]]]:
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            text(
                "SELECT r.run_id, r.trigger_context->>'case_id' "
                f"FROM workflow_runs r WHERE {_ORPHANED}"
            ),
            _params(),
        ).all()
    return [(r[0], r[1]) for r in rows]


def _ledger_terminal(run_id: str) -> Optional[Dict[str, Any]]:
    """The terminal the run's ledger holds, if it journaled one."""
    with get_db_manager().session_scope() as session:
        row = session.execute(
            text(
                "SELECT payload FROM agent_events "
                "WHERE run_id::text = :run_id AND kind = 'terminal' "
                "ORDER BY seq DESC LIMIT 1"
            ),
            {"run_id": run_id},
        ).first()
    return dict(row[0]) if row is not None else None


def _claim(run_id: str, status: str) -> bool:
    """Move the run out of flight, if it is still an orphan. One caller wins: the
    status filter and the lease conditions are re-read by the write itself."""
    with get_db_manager().session_scope() as session:
        won = session.execute(
            text(
                "UPDATE workflow_runs r SET status = :status, stop_cause = :cause "
                f"WHERE r.run_id = :run_id AND {_ORPHANED} "
                "RETURNING r.run_id"
            ),
            {
                **_params(),
                "run_id": run_id,
                "status": status,
                "cause": ORPHANED_STOP_CAUSE,
            },
        ).first()
    return won is not None


def end_orphaned_runs() -> int:
    """End each orphaned run; returns how many this call ended.

    A run with no terminal in its ledger fails. One that has a terminal takes it,
    as the report that never arrived would have set it. Approvals, the ledger and
    the run row are written as when the worker ends a run itself.
    """
    from core.response.checkpoints import withdraw_for_run
    from core.workflows.run_bridge_router import TERMINAL_STATUS, _add_activity
    from core.workflows.run_cancel import force_terminal

    runs = WorkflowRunService()
    ended = 0
    for run_id, case_id in _orphans():
        held = _ledger_terminal(run_id)
        outcome = str(held.get("outcome")) if held else "failed"
        status = TERMINAL_STATUS.get(outcome, "failed") if held else "failed"
        reason = (str(held.get("reason")) if held else "") or None
        if not held:
            reason = REASON
        if not _claim(run_id, status):
            continue
        withdraw_for_run(run_id, reason or REASON)
        if not held:
            force_terminal(run_id, REASON, outcome="failed")
        runs.finalize_run(
            run_id,
            status=status,
            error=reason if status == "failed" else None,
            outcome=outcome,
            reason=reason,
            stop_cause=ORPHANED_STOP_CAUSE,
        )
        ended += 1
        logger.warning("ended orphaned run %s as %s", run_id, status)
        if case_id and not held:
            try:
                _add_activity(
                    case_id,
                    "agent_run_report",
                    REASON,
                    {"run_id": run_id, "outcome": "failed"},
                )
            except Exception:  # noqa: BLE001 - the run ended either way
                logger.exception("could not record %s on case %s", run_id, case_id)
    return ended
