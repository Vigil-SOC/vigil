"""Enqueue agent runs onto the queue the TypeScript agent layer consumes (GH #590).

The backend enqueues plain JSON and never writes ``agent_events``: the agent
layer owns that table and is its single writer (ADR-0001). The payload contract
lives in ``ai/contracts/job.ts`` and is mirrored here, so a change on either side
is a change to both.

BullMQ rather than ARQ because ARQ is a Python library the TypeScript worker
cannot consume. The Python and Node BullMQ libraries are separately versioned
lines that agree on a key layout and a set of Lua scripts; both are exact pins,
and the walking-skeleton integration test is what proves they still agree.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from bullmq import Queue

from core.config import get_settings

logger = logging.getLogger(__name__)

# No colon: the Node library refuses a queue name containing one, while the
# Python library accepts it and writes the keys anyway. Keys are bull:agent-runs:*.
RUN_QUEUE = "agent-runs"

JOB_SCHEMA_VERSION = 1

DEFAULT_REDIS_URL = "redis://localhost:6379/0"

RUN_KINDS = ("hunt", "investigate", "compose", "chat")


def _redis_url() -> str:
    return get_settings().redis_url or DEFAULT_REDIS_URL


def build_start_job(
    run_id: str,
    run_kind: str,
    request: Dict[str, Any],
    enqueued_by: str,
    tenant_id: Optional[str] = None,
) -> Dict[str, Any]:
    """The ``reason="start"`` arm of the RunJob union in ai/contracts/job.ts."""
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


async def enqueue_run(job: Dict[str, Any]) -> str:
    """Enqueue a run and return its job id.

    jobId is the run_id, so a double POST dedupes inside BullMQ rather than in
    application code.
    """
    queue = Queue(RUN_QUEUE, {"connection": _redis_url()})
    try:
        enqueued = await queue.add("run", job, {"jobId": job["run_id"]})
        logger.info("enqueued agent run %s (%s)", job["run_id"], job["run_kind"])
        return str(enqueued.id)
    finally:
        await queue.close()


def new_run_id() -> str:
    return str(uuid.uuid4())
