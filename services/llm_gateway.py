"""Central LLM Gateway -- routes all Claude API calls through ARQ job queues.

All components (daemon processor, agent runner, backend API, AI insights)
enqueue LLM requests here instead of calling Claude directly. This provides:
  - Priority queuing (triage > investigation > chat > insights)
  - Global Anthropic rate-limit enforcement
  - Persistent chat session isolation via Redis
  - Job persistence and automatic retries
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from arq.jobs import DeserializationError

logger = logging.getLogger(__name__)

QUEUE_NAME = "arq:llm"

DEFAULT_REDIS_URL = "redis://localhost:6379/0"


def _redis_settings() -> RedisSettings:
    url = os.getenv("REDIS_URL", DEFAULT_REDIS_URL)
    # Parse redis://host:port/db
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
# Gateway -- singleton entry point used by all callers
# ---------------------------------------------------------------------------

@dataclass
class LLMRequest:
    """Describes a single LLM call to be queued."""
    messages: List[Dict]
    model: str = "claude-sonnet-4-5-20250929"
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

    Usage::

        gateway = await LLMGateway.create()
        result = await gateway.submit_triage("Analyze this finding ...")
    """

    def __init__(self, redis_pool: ArqRedis):
        self._pool = redis_pool
        self.session_store = RedisSessionStore(redis_pool)

    @classmethod
    async def create(cls, settings: Optional[RedisSettings] = None) -> "LLMGateway":
        settings = settings or _redis_settings()
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

    # -- Convenience enqueue methods ----------------------------------------

    async def submit_triage(
        self,
        prompt: str,
        *,
        model: str = "claude-sonnet-4-5-20250929",
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
            traceparent=self._get_traceparent(),
            _queue_name=QUEUE_NAME,
        )
        try:
            return await job.result(timeout=timeout)
        except DeserializationError as exc:
            logger.error(
                "arq job result deserialization failed (stale or incompatible "
                "result in Redis — APIStatusError constructor may have changed): %s",
                exc,
            )
            raise RuntimeError(f"LLM job result deserialization failed: {exc}") from exc

    async def submit_investigation(
        self,
        inv_id: str,
        prompt: str,
        *,
        model: str = "claude-sonnet-4-5-20250929",
        max_tokens: int = 4096,
        enable_thinking: bool = True,
        thinking_budget: int = 8000,
        tools: Optional[List[Dict]] = None,
        timeout: int = 180,
    ) -> Dict[str, Any]:
        """Enqueue an investigation LLM call (medium priority).

        Returns the full response dict including token counts so the
        agent runner can track cost.
        """
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
            traceparent=self._get_traceparent(),
            _queue_name=QUEUE_NAME,
        )
        try:
            return await job.result(timeout=timeout)
        except DeserializationError as exc:
            logger.error(
                "arq job result deserialization failed for investigation job: %s", exc
            )
            raise RuntimeError(f"LLM job result deserialization failed: {exc}") from exc

    async def submit_investigation_turn(
        self,
        inv_id: str,
        messages: List[Dict],
        *,
        model: str = "claude-sonnet-4-5-20250929",
        max_tokens: int = 16000,
        enable_thinking: bool = True,
        thinking_budget: int = 8000,
        tools: Optional[List[Dict]] = None,
        timeout: int = 180,
    ) -> Dict[str, Any]:
        """Enqueue a multi-turn investigation call with explicit messages.

        Used by AgentRunner's tool-use loop where messages contain
        assistant + tool_result turns.
        """
        job = await self._pool.enqueue_job(
            "llm_call_raw",
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            enable_thinking=enable_thinking,
            thinking_budget=thinking_budget,
            tools=tools,
            temperature=None,
            traceparent=self._get_traceparent(),
            _queue_name=QUEUE_NAME,
        )
        try:
            return await job.result(timeout=timeout)
        except DeserializationError as exc:
            logger.error(
                "arq job result deserialization failed for investigation_turn job: %s",
                exc,
            )
            raise RuntimeError(f"LLM job result deserialization failed: {exc}") from exc

    async def submit_chat(
        self,
        messages: List[Dict],
        *,
        session_id: Optional[str] = None,
        model: str = "claude-sonnet-4-20250514",
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
            traceparent=self._get_traceparent(),
            _queue_name=QUEUE_NAME,
        )
        try:
            return await job.result(timeout=timeout)
        except DeserializationError as exc:
            logger.error(
                "arq job result deserialization failed for chat job: %s", exc
            )
            raise RuntimeError(f"LLM job result deserialization failed: {exc}") from exc

    async def submit_insights(
        self,
        prompt: str,
        *,
        model: str = "claude-sonnet-4-20250514",
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
            traceparent=self._get_traceparent(),
            _queue_name=QUEUE_NAME,
        )
        try:
            return await job.result(timeout=timeout)
        except DeserializationError as exc:
            logger.error(
                "arq job result deserialization failed for insights job: %s", exc
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
