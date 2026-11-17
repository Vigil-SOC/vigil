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
and only the report to the console was lost, so that terminal is replayed through
the same code as the worker's own report.
A run ended here gets a terminal in its ledger, even when it has no ledger yet, so
a worker that later dequeues the lost start job refuses to run it. Its row is also
sealed (``stop_cause``) against a late terminal.

The lease timings below are the agent layer's defaults. They are read from this
process's environment, which no deployment config sets: an operator who overrides
VIGIL_PARK_POLL_MS, VIGIL_DENIAL_RECHECK_MS, VIGIL_SWEEP_MS or VIGIL_LEASE_TTL_MS
on the agent must give the daemon the same values.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError
from sqlalchemy import text

from core.storage.connection import get_db_manager
from core.workflows.workflow_run_service import (
    DURATION_MS_MAX,
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


def _uuid(run_id: str) -> bool:
    try:
        uuid.UUID(run_id)
    except ValueError:
        return False
    return True


def _orphans() -> List[Tuple[str, Optional[str], Optional[str]]]:
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            text(
                "SELECT r.run_id, r.trigger_context->>'case_id', "
                "       r.trigger_context->>'run_kind' "
                f"FROM workflow_runs r WHERE {_ORPHANED}"
            ),
            _params(),
        ).all()
    return [(r[0], r[1], r[2]) for r in rows]


def _ledger_terminal(run_id: str) -> Optional[Dict[str, Any]]:
    """The terminal the run's ledger holds, if it journaled one."""
    if not _uuid(run_id):
        return None
    with get_db_manager().session_scope() as session:
        row = session.execute(
            text(
                "SELECT payload FROM agent_events "
                "WHERE run_id = CAST(:run_id AS uuid) AND kind = 'terminal' "
                "ORDER BY seq LIMIT 1"
            ),
            {"run_id": run_id},
        ).first()
    return dict(row[0]) if row is not None else None


def _claim(run_id: str) -> bool:
    """End the run as failed, if it is still an orphan, with the row complete.
    One caller wins: the write re-reads the status and lease conditions itself."""
    with get_db_manager().session_scope() as session:
        won = session.execute(
            text(
                "UPDATE workflow_runs r SET status = 'failed', "
                "  stop_cause = :cause, outcome = 'failed', error = :reason, "
                "  reason = :reason, finished_at = timezone('utc', now()), "
                "  duration_ms = CASE WHEN r.started_at IS NULL THEN r.duration_ms "
                "    WHEN extract(epoch FROM timezone('utc', now()) - r.started_at) "
                "         * 1000 > :int4_max THEN NULL "
                "    ELSE (extract(epoch FROM timezone('utc', now()) "
                "          - r.started_at) * 1000)::int END "
                f"WHERE r.run_id = :run_id AND {_ORPHANED} "
                "RETURNING r.run_id"
            ),
            {
                **_params(),
                "run_id": run_id,
                "cause": ORPHANED_STOP_CAUSE,
                "reason": REASON,
                "int4_max": DURATION_MS_MAX,
            },
        ).first()
    return won is not None


def _replay(run_id: str, held: Dict[str, Any]) -> None:
    """Deliver the terminal the worker journaled and the report lost."""
    from core.response.approval_service import ApprovalService
    from core.workflows.run_bridge_router import TerminalUpdate, apply_terminal

    try:
        update = TerminalUpdate(
            **{k: v for k, v in held.items() if k in TerminalUpdate.model_fields}
        )
    except ValidationError:
        logger.warning("terminal of %s is not replayable whole", run_id)
        update = TerminalUpdate(
            outcome=str(held.get("outcome") or "failed"),
            reason=str(held.get("reason") or ""),
        )
    # No MCP registry in the daemon: a handoff's root-cause run is not started here.
    apply_terminal(run_id, update, WorkflowRunService(), ApprovalService(), None)


def end_orphaned_runs() -> int:
    """End each orphaned run; returns how many this call ended or replayed.

    A run with no terminal in its ledger is failed. One that has a terminal gets
    it replayed, as the report that never arrived would have. A failed run has a
    terminal journaled first (so no worker takes it up later), then its row
    completed in one write, then its approvals withdrawn and its case told.
    """
    from core.response.checkpoints import withdraw_for_run
    from core.workflows.run_bridge_router import _add_activity
    from core.workflows.run_cancel import force_terminal
    from core.workflows.workflow_run_service import _runs_finished_counter

    ended = 0
    for run_id, case_id, run_kind in _orphans():
        held = _ledger_terminal(run_id)
        if held is not None:
            _replay(run_id, held)
            ended += 1
            logger.warning("replayed the terminal of orphaned run %s", run_id)
            continue
        # A console id that is not a uuid has no ledger and no worker to refuse. A
        # run with no run_kind was never enqueued by a known path, so there is no
        # job to refuse; it is ended without one.
        if _uuid(run_id):
            written = force_terminal(
                run_id, REASON, outcome="failed", run_kind=run_kind
            )
            if not written and run_kind:
                logger.error("could not journal a terminal for orphaned run %s", run_id)
                continue
        if not _claim(run_id):
            continue
        ended += 1
        logger.warning("ended orphaned run %s", run_id)
        _runs_finished_counter().add(
            1, {"run_kind": str(run_kind or "unknown"), "status": "failed"}
        )
        try:
            withdraw_for_run(run_id, REASON)
        except Exception:  # noqa: BLE001 - the run ended either way
            logger.exception("could not withdraw the approvals of %s", run_id)
        if case_id:
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
