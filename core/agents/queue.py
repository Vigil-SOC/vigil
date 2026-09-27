# Enqueue agent runs onto the queue the TypeScript agent layer consumes. The
# backend enqueues plain JSON and never writes agent_events.

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from weakref import WeakKeyDictionary

from bullmq import Queue

from core.config import get_settings
from core.telemetry import get_meter

logger = logging.getLogger(__name__)

# No colon: the Node library refuses a queue name containing one, while the
# Python library accepts it and writes the keys anyway. Keys are bull:agent-runs:*.
RUN_QUEUE = "agent-runs"

JOB_SCHEMA_VERSION = 1

DEFAULT_REDIS_URL = "redis://localhost:6379/0"

RUN_KINDS = ("hunt", "root_cause", "adjudicate", "investigate", "compose", "chat")

# BullMQ defaults to one attempt, so a job that throws is permanently failed and
# nothing rescues it: the watchdog sweeps lapsed lease rows, and a job that died on
# its way into leases.claim never wrote one. On a dev box a transient Postgres or
# Redis failure is rare; under Kubernetes -- rolling upgrades, evictions, failover --
# it is routine, and the run would be lost with nothing reaching the console.
#
# Retrying is safe rather than merely tolerable: advance() checks terminal first and
# leases.claim is a conditional UPDATE, so a second attempt takes exactly the path a
# watchdog resume takes -- reachable because a failed attempt hands its lease back
# (services/agent/worker.ts::forget).
#
# The consumer reads these off the job, so the Node worker honours what is set here.
RUN_ATTEMPTS = 3
RUN_BACKOFF = {"type": "exponential", "delay": 5000}


def _redis_url() -> str:
    return get_settings().redis_url or DEFAULT_REDIS_URL


# The reason="start" arm of the RunJob union in the agent layer's job contract.
def build_start_job(
    run_id: str,
    run_kind: str,
    request: Dict[str, Any],
    enqueued_by: str,
    tenant_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "schema_version": JOB_SCHEMA_VERSION,
        "run_id": run_id,
        "run_kind": run_kind,
        "tenant_id": tenant_id,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
        "enqueued_by": enqueued_by,
        "reason": "start",
        "request": request,
    }


# A resume carries no request: the ledger holds the spec, and what unblocks the
# run is the decision the agent layer reads back, not anything said here.
def build_resume_job(
    run_id: str,
    run_kind: str,
    enqueued_by: str,
    tenant_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "schema_version": JOB_SCHEMA_VERSION,
        "run_id": run_id,
        "run_kind": run_kind,
        "tenant_id": tenant_id,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
        "enqueued_by": enqueued_by,
        "reason": "resume",
    }


_queues: "WeakKeyDictionary[asyncio.AbstractEventLoop, Queue]" = WeakKeyDictionary()

_runs_started: Any = None


# Lazy, not at import: get_meter before init_telemetry hands back a permanent no-op.
def _runs_started_counter() -> Any:
    global _runs_started
    if _runs_started is None:
        _runs_started = get_meter("vigil.agents.queue").create_counter(
            "vigil.runs.started",
            description="Agent runs enqueued to start, by run_kind",
            unit="1",
        )
    return _runs_started


# One per event loop, not per call: a Queue per enqueue costs a Redis connection.
# Keyed by loop because its connection is bound to the one that made it.
def _run_queue() -> Queue:
    loop = asyncio.get_running_loop()
    queue = _queues.get(loop)
    if queue is None:
        queue = Queue(RUN_QUEUE, {"connection": _redis_url()})
        _queues[loop] = queue
    return queue


# Called on shutdown, so the connection does not outlive the loop that made it.
async def close_run_queue() -> None:
    queue = _queues.pop(asyncio.get_running_loop(), None)
    if queue is not None:
        await queue.close()


async def enqueue_run(job: Dict[str, Any], job_id: Optional[str] = None) -> str:
    queue = _run_queue()
    try:
        # jobId is the run id for a start, so a double POST dedupes in BullMQ. A
        # resume takes a fresh id: any derived one repeats, and the queue drops it.
        enqueued = await queue.add(
            "run",
            job,
            {
                "jobId": job_id or _default_job_id(job),
                "attempts": RUN_ATTEMPTS,
                "backoff": RUN_BACKOFF,
            },
        )
        logger.info("enqueued agent run %s (%s)", job["run_id"], job["run_kind"])
        # A resume re-enters a run that was already counted when it started.
        if job.get("reason") == "start":
            _runs_started_counter().add(1, {"run_kind": job["run_kind"]})
        return str(enqueued.id)
    except Exception:
        # A queue that failed is not reused: the next call builds a fresh one
        # rather than inheriting a connection that may already be gone.
        await close_run_queue()
        raise


def _default_job_id(job: Dict[str, Any]) -> str:
    if job.get("reason") == "start":
        return str(job["run_id"])
    return f"{job['run_id']}:{uuid.uuid4()}"


def new_run_id() -> str:
    return str(uuid.uuid4())
