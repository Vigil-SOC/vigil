"""Central LLM Gateway -- routes all Claude API calls through ARQ job queues.

All components (daemon processor, agent runner, backend API, AI insights)
enqueue LLM requests here instead of calling Claude directly. This provides:
  - Priority queuing (triage > investigation > chat > insights)
  - Per-queue concurrency limits enforced by separate workers
  - Global Anthropic rate-limit enforcement with exponential backoff on 429s
  - Persistent chat session isolation via Redis
  - Dead-letter queue (DLQ) for failed jobs with replay support
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Queue name constants — each maps to a dedicated worker process.
# Workers are started with LLM_WORKER_QUEUE=<name> (see WorkerSettings below).
# Priority order: triage > investigation > chat > insights
# ---------------------------------------------------------------------------

QUEUE_TRIAGE      = "arq:llm:triage"
QUEUE_INVESTIGATION = "arq:llm:investigation"
QUEUE_CHAT        = "arq:llm:chat"
QUEUE_INSIGHTS    = "arq:llm:insights"

# Legacy alias so imports of QUEUE_NAME keep working
QUEUE_NAME = QUEUE_CHAT

# ---------------------------------------------------------------------------
# Compatibility / single-queue mode
#
# Set LLM_SINGLE_QUEUE=true (or LLM_WORKER_QUEUE to any non-empty value) to
# route ALL requests to the single queue specified by LLM_WORKER_QUEUE
# (default: arq:llm:chat).  This lets a single worker drain every job type,
# which is useful in development or resource-constrained environments where
# you don't want to run four separate worker processes.
# ---------------------------------------------------------------------------
_single_queue_env = os.getenv("LLM_SINGLE_QUEUE", "").lower()
_worker_queue_env = os.getenv("LLM_WORKER_QUEUE", "")
SINGLE_QUEUE_MODE: bool = _single_queue_env in ("1", "true", "yes") or bool(_worker_queue_env)
_COMPAT_QUEUE: str = _worker_queue_env or QUEUE_CHAT


def _resolve_queue(preferred: str) -> str:
    """Return the actual queue name, respecting single-queue compatibility mode."""
    return _COMPAT_QUEUE if SINGLE_QUEUE_MODE else preferred

# Dead-letter queue key (Redis list, not an ARQ queue)
DLQ_KEY = "arq:llm:dlq"

DEFAULT_REDIS_URL = "redis://localhost:6379/0"


def _redis_settings() -> RedisSettings:
    url = os.getenv("REDIS_URL", DEFAULT_REDIS_URL)
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=int(parsed.path.lstrip("/") or 0),
        password=parsed.password,
    )


# ---------------------------------------------------------------------------
# Session store -- keeps chat histories isolated per session_id in Redis
# ---------------------------------------------------------------------------

class RedisSessionStore:
    """Stores per-session message histories in Redis with TTL."""

    DEFAULT_TTL = 60 * 60 * 4  # 4 hours

    def __init__(self, redis: ArqRedis, ttl: int = DEFAULT_TTL):
        self.redis = redis
        self.ttl = ttl

    def _key(self, session_id: str) -> str:
        return f"llm:session:{session_id}"

    async def load(self, session_id: str) -> List[Dict]:
        raw = await self.redis.get(self._key(session_id))
        if raw is None:
            return []
        return json.loads(raw)

    async def save(self, session_id: str, messages: List[Dict]):
        await self.redis.set(
            self._key(session_id),
            json.dumps(messages, default=str),
            ex=self.ttl,
        )

    async def delete(self, session_id: str):
        await self.redis.delete(self._key(session_id))

    async def exists(self, session_id: str) -> bool:
        return bool(await self.redis.exists(self._key(session_id)))

    async def touch(self, session_id: str):
        """Reset TTL without modifying data."""
        await self.redis.expire(self._key(session_id), self.ttl)


# ---------------------------------------------------------------------------
# Dead-letter queue helpers
# ---------------------------------------------------------------------------

class DeadLetterQueue:
    """Stores permanently-failed LLM jobs for inspection and replay."""

    MAX_ENTRIES = 1000  # Cap to avoid unbounded growth

    def __init__(self, redis: ArqRedis):
        self._redis = redis

    async def push(self, job_meta: Dict[str, Any]):
        """Record a failed job."""
        entry = json.dumps(job_meta, default=str)
        pipe = self._redis.pipeline()
        pipe.lpush(DLQ_KEY, entry)
        pipe.ltrim(DLQ_KEY, 0, self.MAX_ENTRIES - 1)
        await pipe.execute()

    async def list(self, limit: int = 50) -> List[Dict]:
        """Return up to *limit* most-recent DLQ entries."""
        raw_items = await self._redis.lrange(DLQ_KEY, 0, limit - 1)
        return [json.loads(r) for r in raw_items]

    async def clear(self):
        await self._redis.delete(DLQ_KEY)

    async def length(self) -> int:
        return await self._redis.llen(DLQ_KEY)


# ---------------------------------------------------------------------------
# Gateway -- singleton entry point used by all callers
# ---------------------------------------------------------------------------

@dataclass
class LLMRequest:
    """Describes a single LLM call to be queued."""
    messages: List[Dict]
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 4096
    system_prompt: Optional[str] = None
    session_id: Optional[str] = None
    enable_thinking: bool = False
    thinking_budget: int = 10000
    tools: Optional[List[Dict]] = None
    temperature: Optional[float] = None
    extra_kwargs: Dict[str, Any] = field(default_factory=dict)


class LLMGateway:
    """Enqueues LLM requests into ARQ priority queues.

    **Multi-queue mode (default):** Each priority tier is a separate named queue
    consumed by a dedicated worker process.  Start all four workers with::

        scripts/start_llm_workers.sh

    or individually::

        LLM_WORKER_QUEUE=arq:llm:triage      python -m arq services.llm_worker.WorkerSettings
        LLM_WORKER_QUEUE=arq:llm:investigation python -m arq services.llm_worker.WorkerSettings
        LLM_WORKER_QUEUE=arq:llm:chat         python -m arq services.llm_worker.WorkerSettings
        LLM_WORKER_QUEUE=arq:llm:insights     python -m arq services.llm_worker.WorkerSettings

    **Single-queue compatibility mode:** Set ``LLM_SINGLE_QUEUE=true`` (or set
    ``LLM_WORKER_QUEUE`` to the desired queue name) to route *all* job types to a
    single queue.  A single worker then drains everything, which is convenient for
    development or resource-constrained environments::

        LLM_SINGLE_QUEUE=true python -m arq services.llm_worker.WorkerSettings

    Usage::

        gateway = await LLMGateway.create()
        result = await gateway.submit_triage("Analyze this finding ...")
    """

    def __init__(self, redis_pool: ArqRedis):
        self._pool = redis_pool
        self.session_store = RedisSessionStore(redis_pool)
        self.dlq = DeadLetterQueue(redis_pool)

    @classmethod
    async def create(cls, settings: Optional[RedisSettings] = None) -> "LLMGateway":
        settings = settings or _redis_settings()
        pool = await create_pool(settings)
        return cls(pool)

    async def close(self):
        if self._pool:
            await self._pool.aclose()

    # -- Convenience enqueue methods ----------------------------------------

    async def submit_triage(
        self,
        prompt: str,
        *,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 2048,
        timeout: int = 90,
    ) -> Optional[str]:
        """Enqueue a stateless triage call (highest priority)."""
        job = await self._pool.enqueue_job(
            "llm_call",
            messages=[{"role": "user", "content": prompt}],
            model=model,
            max_tokens=max_tokens,
            session_id=None,
            system_prompt=None,
            enable_thinking=False,
            thinking_budget=0,
            tools=None,
            temperature=None,
            job_type="triage",
            _queue_name=_resolve_queue(QUEUE_TRIAGE),
        )
        return await job.result(timeout=timeout)

    async def submit_investigation(
        self,
        inv_id: str,
        prompt: str,
        *,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 4096,
        enable_thinking: bool = True,
        thinking_budget: int = 8000,
        tools: Optional[List[Dict]] = None,
        timeout: int = 180,
    ) -> Dict[str, Any]:
        """Enqueue an investigation LLM call (high priority)."""
        job = await self._pool.enqueue_job(
            "llm_call",
            messages=[{"role": "user", "content": prompt}],
            model=model,
            max_tokens=max_tokens,
            session_id=f"inv:{inv_id}",
            system_prompt=None,
            enable_thinking=enable_thinking,
            thinking_budget=thinking_budget,
            tools=tools,
            temperature=None,
            job_type="investigation",
            _queue_name=_resolve_queue(QUEUE_INVESTIGATION),
        )
        return await job.result(timeout=timeout)

    async def submit_investigation_turn(
        self,
        inv_id: str,
        messages: List[Dict],
        *,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 16000,
        enable_thinking: bool = True,
        thinking_budget: int = 8000,
        tools: Optional[List[Dict]] = None,
        timeout: int = 180,
    ) -> Dict[str, Any]:
        """Enqueue a multi-turn investigation call with explicit messages."""
        job = await self._pool.enqueue_job(
            "llm_call_raw",
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            enable_thinking=enable_thinking,
            thinking_budget=thinking_budget,
            tools=tools,
            temperature=None,
            job_type="investigation",
            _queue_name=_resolve_queue(QUEUE_INVESTIGATION),
        )
        return await job.result(timeout=timeout)

    async def submit_chat(
        self,
        messages: List[Dict],
        *,
        session_id: Optional[str] = None,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 4096,
        system_prompt: Optional[str] = None,
        enable_thinking: bool = False,
        thinking_budget: int = 10000,
        timeout: int = 120,
    ) -> Any:
        """Enqueue a UI chat call (normal priority)."""
        job = await self._pool.enqueue_job(
            "llm_call",
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            session_id=session_id,
            system_prompt=system_prompt,
            enable_thinking=enable_thinking,
            thinking_budget=thinking_budget,
            tools=None,
            temperature=None,
            job_type="chat",
            _queue_name=_resolve_queue(QUEUE_CHAT),
        )
        return await job.result(timeout=timeout)

    async def submit_insights(
        self,
        prompt: str,
        *,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 2000,
        temperature: float = 0.3,
        timeout: int = 90,
    ) -> Optional[str]:
        """Enqueue a background insights/analytics call (lowest priority)."""
        job = await self._pool.enqueue_job(
            "llm_call",
            messages=[{"role": "user", "content": prompt}],
            model=model,
            max_tokens=max_tokens,
            session_id=None,
            system_prompt=None,
            enable_thinking=False,
            thinking_budget=0,
            tools=None,
            temperature=temperature,
            job_type="insights",
            _queue_name=_resolve_queue(QUEUE_INSIGHTS),
        )
        return await job.result(timeout=timeout)


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
