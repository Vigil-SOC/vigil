"""LLMGateway.submit drops a job once its caller has stopped waiting.

The expiry has to be set when the job is enqueued: the daemon wraps the whole
triage call in ``asyncio.wait_for`` with the same timeout it passes to
``submit``, so the coroutine is usually cancelled before ``submit`` could clean
up in an ``except TimeoutError``.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.llm.gateway.gateway import QUEUE_NAME, LLMGateway


def _gateway() -> tuple[LLMGateway, AsyncMock]:
    job = MagicMock()
    job.result = AsyncMock(return_value={"text": "ok"})
    pool = MagicMock()
    pool.enqueue_job = AsyncMock(return_value=job)
    return LLMGateway(pool), pool.enqueue_job


def test_submit_expires_job_at_default_timeout():
    gateway, enqueue_job = _gateway()
    asyncio.run(gateway.submit("hello"))
    kwargs = enqueue_job.call_args.kwargs
    assert kwargs["_expires"] == 90
    assert kwargs["_queue_name"] == QUEUE_NAME


@pytest.mark.parametrize("timeout", [2, 45, 300])
def test_submit_expires_job_at_explicit_timeout(timeout):
    gateway, enqueue_job = _gateway()
    asyncio.run(gateway.submit("hello", timeout=timeout))
    assert enqueue_job.call_args.kwargs["_expires"] == timeout
