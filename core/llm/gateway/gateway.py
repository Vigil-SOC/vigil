"""LLM Gateway -- enqueues stateless completions onto the ARQ ``arq:llm`` queue.

The daemon's triage and the AI-insights refresh call through here; the
llm-worker drains the queue under a shared cap on calls in flight, with ARQ's
job persistence and retries. A job no worker has started within its timeout is
dropped by the worker, not run late: ``submit`` sets the job's expiry to it.
"""

import asyncio
import logging
from typing import Any, Dict, Optional

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from arq.jobs import DeserializationError, JobStatus

from core.llm.defaults import DEFAULT_MODEL
from core.redis_client import arq_redis_settings

logger = logging.getLogger(__name__)

QUEUE_NAME = "arq:llm"
# The llm-worker's limit on one call once it has started it.
JOB_TIMEOUT = 180


def redis_settings() -> RedisSettings:
    """ARQ connection settings for the gateway and the llm-worker.

    Public: the llm-worker builds its WorkerSettings from the same values, so
    the queue it drains is the one the gateway enqueues onto. Parsing, password
    decoding and the slow-start retries live in ``core.redis_client``.
    """
    return arq_redis_settings()


# ---------------------------------------------------------------------------
# Gateway -- singleton entry point used by all callers
# ---------------------------------------------------------------------------


class LLMGateway:
    """Enqueues LLM requests onto the ARQ queue.

    Usage::

        gateway = await LLMGateway.create()
        result = await gateway.submit("Analyze this finding ...")
    """

    def __init__(self, redis_pool: ArqRedis):
        self._pool = redis_pool

    @classmethod
    async def create(cls, settings: Optional[RedisSettings] = None) -> "LLMGateway":
        settings = settings or redis_settings()
        pool = await create_pool(settings)

        # Instrument the underlying Redis client with OTEL tracing
        try:
            from opentelemetry.instrumentation.redis import RedisInstrumentor

            RedisInstrumentor().instrument()
            logger.debug("Redis OTEL instrumentation enabled")
        except Exception as _inst_err:
            logger.debug("Redis OTEL instrumentation skipped: %s", _inst_err)

        return cls(pool)

    async def close(self):
        if self._pool:
            await self._pool.aclose()

    # -- Trace context helpers -----------------------------------------------

    @staticmethod
    def _get_traceparent() -> str:
        """Capture the current W3C traceparent for ARQ job propagation."""
        try:
            from core.telemetry import inject_traceparent

            carrier: Dict[str, str] = {}
            inject_traceparent(carrier)
            return carrier.get("traceparent", "")
        except Exception:
            return ""

    async def submit(
        self,
        prompt: str,
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 2048,
        temperature: Optional[float] = None,
        timeout: int = 90,
        provider_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Enqueue one stateless completion and wait for its result.

        The job expires after ``timeout`` seconds. A worker that picks it up
        later skips it without a model call, so a caller that gave up leaves
        no stale work behind. A job a worker has already started runs to the
        end whether anyone waits or not, so its result is waited for, up to
        ``JOB_TIMEOUT``, instead of being thrown away. A local model can spend
        most of ``timeout`` loading before it answers.
        """
        job = await self._pool.enqueue_job(
            "llm_call",
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            provider_id=provider_id,
            traceparent=self._get_traceparent(),
            _queue_name=QUEUE_NAME,
            _expires=timeout,
        )
        try:
            try:
                return await job.result(timeout=timeout)
            except asyncio.TimeoutError:
                if await job.status() not in (
                    JobStatus.in_progress,
                    JobStatus.complete,
                ):
                    raise
                return await job.result(timeout=JOB_TIMEOUT)
        except DeserializationError as exc:
            logger.error(
                "arq job result deserialization failed (stale or incompatible "
                "result in Redis — APIStatusError constructor may have changed): %s",
                exc,
            )
            raise RuntimeError(f"LLM job result deserialization failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_gateway: Optional[LLMGateway] = None
_gateway_lock = asyncio.Lock()


async def get_llm_gateway() -> LLMGateway:
    """Return (or create) the module-level LLMGateway singleton."""
    global _gateway
    if _gateway is not None:
        return _gateway
    async with _gateway_lock:
        if _gateway is not None:
            return _gateway
        _gateway = await LLMGateway.create()
        logger.info("LLMGateway initialized (connected to Redis)")
        return _gateway


async def close_llm_gateway():
    """Shut down the gateway (call on app shutdown)."""
    global _gateway
    if _gateway:
        await _gateway.close()
        _gateway = None
        logger.info("LLMGateway closed")
