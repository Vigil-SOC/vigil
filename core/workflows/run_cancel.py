"""Stopping a run, in two steps.

The directive is cooperative and lets the hunt settle itself, which is the only
way to get a report out of it. The terminal is the backstop for a worker that is
dead, wedged or failing deterministically: worker.ts short-circuits on one, which
also stops the sweeper re-enqueuing the run forever.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, Optional, Set

from sqlalchemy import text

from core.agents.directives import RunAlreadyEnded, UnknownRun, enqueue_directive
from core.storage.connection import get_db_session

logger = logging.getLogger(__name__)

# How long the run gets to stop itself before one is written for it: an iteration
# boundary and the 500ms abort poll, without leaving an operator watching spend.
ESCALATE_AFTER_S = 30.0

# asyncio holds only a weak reference to a bare task, so an escalation left to the
# garbage collector may never run.
_pending: Set["asyncio.Task[None]"] = set()

EVENT_SCHEMA_VERSION = 1


def _is_run_id(run_id: str) -> bool:
    try:
        uuid.UUID(run_id)
    except ValueError:
        return False
    return True


# Cooperative. A run with no ledger or an already-journaled terminal is not a failure
# here: the row still wants finalising, which is the caller's job.
def request_stop(run_id: str, reason: str, actor: str) -> bool:
    if not _is_run_id(run_id):
        return False
    try:
        with get_db_session() as session:
            enqueue_directive(session, run_id, "abort", reason, actor)
            session.commit()
        return True
    except (UnknownRun, RunAlreadyEnded) as exc:
        logger.info("no abort queued for %s: %s", run_id, exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not queue abort for %s: %s", run_id, exc)
    return False


# The backstop. Idempotent by the terminal's own index, so a run that settled itself
# keeps its outcome and its report.
def force_terminal(run_id: str, reason: str) -> bool:
    if not _is_run_id(run_id):
        return False
    try:
        with get_db_session() as session:
            written = session.execute(
                text(
                    "INSERT INTO agent_events "
                    "(run_id, run_kind, seq, kind, payload, schema_version) "
                    "SELECT CAST(:run_id AS uuid), "
                    "  (SELECT run_kind FROM agent_events "
                    "   WHERE run_id = CAST(:run_id AS uuid) ORDER BY seq LIMIT 1), "
                    "  coalesce((SELECT max(seq) FROM agent_events "
                    "            WHERE run_id = CAST(:run_id AS uuid)), -1) + 1, "
                    "  'terminal', CAST(:payload AS jsonb), :version "
                    "WHERE EXISTS (SELECT 1 FROM agent_events "
                    "              WHERE run_id = CAST(:run_id AS uuid)) "
                    "  AND NOT EXISTS (SELECT 1 FROM agent_events "
                    "                  WHERE run_id = CAST(:run_id AS uuid) "
                    "                    AND kind = 'terminal') "
                    "RETURNING seq"
                ),
                {
                    "run_id": run_id,
                    "payload": json.dumps({"outcome": "aborted", "reason": reason}),
                    "version": EVENT_SCHEMA_VERSION,
                },
            ).one_or_none()
            session.commit()
        if written is not None:
            logger.info("wrote a terminal for %s: it did not stop itself", run_id)
        return written is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not write a terminal for %s: %s", run_id, exc)
        return False


async def _escalate(run_id: str, reason: str, delay: float) -> None:
    await asyncio.sleep(delay)
    await asyncio.to_thread(
        force_terminal, run_id, f"{reason} (did not stop on request)"
    )


# Scheduled rather than awaited: the operator gets an answer now, and the run gets
# its chance to end honestly first.
def escalate_later(
    run_id: str, reason: str, delay: float = ESCALATE_AFTER_S
) -> Optional["asyncio.Task[None]"]:
    try:
        task = asyncio.get_running_loop().create_task(_escalate(run_id, reason, delay))
    except RuntimeError:
        logger.debug("no running loop; %s will not be escalated", run_id)
        return None
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task


# One call for the whole stop: ask, then make sure.
def stop_run(run_id: str, reason: str, actor: str) -> Dict[str, Any]:
    asked = request_stop(run_id, reason, actor)
    if asked:
        escalate_later(run_id, reason)
    return {"abort_queued": asked}
