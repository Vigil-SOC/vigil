# A run enqueued inside a traced request carries that trace to the agent worker,
# whose spans then continue it; with tracing off the job carries nothing extra.

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import core.agents.queue as queue

pytestmark = pytest.mark.unit

TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


async def _enqueued(carrier):
    q = MagicMock()
    q.add = AsyncMock(return_value=SimpleNamespace(id="job-1"))
    with patch.object(queue, "_run_queue", return_value=q), patch.object(
        queue, "inject_traceparent", return_value=carrier
    ):
        await queue.enqueue_run(queue.build_start_job("r1", "hunt", {}, "test"))
    return q.add.call_args.args[1]


async def test_the_job_carries_the_active_trace():
    job = await _enqueued({"traceparent": TRACEPARENT})
    assert job["traceparent"] == TRACEPARENT
    assert job["run_id"] == "r1"


async def test_untraced_the_job_carries_nothing_extra():
    job = await _enqueued({})
    assert "traceparent" not in job
