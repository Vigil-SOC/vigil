"""ARQ worker that processes LLM requests from the queue.

Run with:
    python -m arq services.llm_worker.WorkerSettings

This worker consumes jobs from three priority queues (triage > investigation
> chat). All Claude API calls go through the global rate-limiter semaphore
so we never exceed the Anthropic rate limit regardless of how many callers
are enqueuing concurrently.
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from arq.connections import RedisSettings

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.llm_gateway import (
    QUEUE_NAME,
    RedisSessionStore,
)

logger = logging.getLogger(__name__)

DEFAULT_REDIS_URL = "redis://localhost:6379/0"
MAX_CONCURRENT_LLM_CALLS = int(os.getenv("LLM_MAX_CONCURRENT", "5"))


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
# Worker task functions
# ---------------------------------------------------------------------------


async def llm_call(
    ctx: Dict[str, Any],
    messages: List[Dict],
    model: str,
    max_tokens: int,
    session_id: Optional[str],
    system_prompt: Optional[str],
    enable_thinking: bool,
    thinking_budget: int,
    tools: Optional[List[Dict]],
    temperature: Optional[float],
    traceparent: str = "",
    agent_id: Optional[str] = None,
    investigation_id: Optional[str] = None,
) -> Any:
    """Execute a single LLM call through the shared ClaudeService.

    This is the primary worker function.  It:
      1. Acquires the rate-limit semaphore
      2. Optionally loads session history from Redis
      3. Calls the Anthropic API
      4. Saves updated session history
      5. Returns the response content
    """
    rate_limiter: asyncio.Semaphore = ctx["rate_limiter"]
    claude_service = ctx["claude_service"]
    session_store: RedisSessionStore = ctx["session_store"]

    # Restore parent span context propagated across the ARQ/Redis boundary
    try:
        from core.telemetry import extract_traceparent, get_tracer
        from opentelemetry.trace import SpanKind

        parent_ctx = extract_traceparent({"traceparent": traceparent})
        _tracer = get_tracer("vigil.services.llm_worker")
        worker_span = _tracer.start_span(
            "llm_worker.execute",
            context=parent_ctx,
            kind=SpanKind.CONSUMER,
        )
        worker_span.set_attribute("gen_ai.system", "anthropic")
        worker_span.set_attribute("gen_ai.request.model", model)
    except Exception:
        worker_span = None

    # Load session history if applicable
    if session_id:
        history = await session_store.load(session_id)
        if history:
            messages = history + messages

    await rate_limiter.acquire()
    try:
        response = await asyncio.to_thread(
            _sync_claude_call,
            claude_service,
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
            enable_thinking=enable_thinking,
            thinking_budget=thinking_budget,
            tools=tools,
            temperature=temperature,
            session_id=session_id,
            agent_id=agent_id,
            investigation_id=investigation_id,
        )
    finally:
        rate_limiter.release()

    result = _extract_result(response)

    if worker_span is not None:
        try:
            worker_span.end()
        except Exception:
            pass

    # Persist session
    if session_id:
        updated = messages + [
            {"role": "assistant", "content": result.get("content", "")}
        ]
        await session_store.save(session_id, updated)

    return result


async def llm_call_raw(
    ctx: Dict[str, Any],
    messages: List[Dict],
    model: str,
    max_tokens: int,
    enable_thinking: bool,
    thinking_budget: int,
    tools: Optional[List[Dict]],
    temperature: Optional[float],
    traceparent: str = "",
    investigation_id: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a raw multi-turn LLM call (used by AgentRunner tool loop).

    Unlike ``llm_call``, this does NOT manage sessions -- the caller
    provides the full message list including assistant/tool_result turns.
    Returns the raw Anthropic response as a serialisable dict.
    """
    rate_limiter: asyncio.Semaphore = ctx["rate_limiter"]
    claude_service = ctx["claude_service"]

    # Restore parent span context propagated across the ARQ/Redis boundary
    try:
        from core.telemetry import extract_traceparent, get_tracer
        from opentelemetry.trace import SpanKind

        parent_ctx = extract_traceparent({"traceparent": traceparent})
        _tracer = get_tracer("vigil.services.llm_worker")
        worker_span = _tracer.start_span(
            "llm_worker.execute",
            context=parent_ctx,
            kind=SpanKind.CONSUMER,
        )
        worker_span.set_attribute("gen_ai.system", "anthropic")
        worker_span.set_attribute("gen_ai.request.model", model)
    except Exception:
        worker_span = None

    await rate_limiter.acquire()
    try:
        response = await asyncio.to_thread(
            _sync_claude_raw,
            claude_service,
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            enable_thinking=enable_thinking,
            thinking_budget=thinking_budget,
            tools=tools,
            temperature=temperature,
            investigation_id=investigation_id,
            agent_id=agent_id,
        )
    finally:
        rate_limiter.release()

    result = _serialize_raw_response(response)

    if worker_span is not None:
        try:
            in_tok = result.get("input_tokens", 0)
            out_tok = result.get("output_tokens", 0)
            worker_span.set_attribute("gen_ai.usage.input_tokens", in_tok)
            worker_span.set_attribute("gen_ai.usage.output_tokens", out_tok)
            worker_span.set_attribute(
                "gen_ai.finish_reason", result.get("stop_reason", "")
            )
            worker_span.end()
        except Exception:
            pass

    return result


# ---------------------------------------------------------------------------
# Sync helpers (run inside asyncio.to_thread)
# ---------------------------------------------------------------------------


def _sync_claude_call(
    claude_service,
    *,
    messages: List[Dict],
    model: str,
    max_tokens: int,
    system_prompt: Optional[str],
    enable_thinking: bool,
    thinking_budget: int,
    tools: Optional[List[Dict]],
    temperature: Optional[float],
    session_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    investigation_id: Optional[str] = None,
) -> Any:
    """Call ClaudeService.chat() synchronously."""
    current_message = messages[-1]["content"] if messages else ""
    context = messages[:-1] if len(messages) > 1 else None

    return claude_service.chat(
        message=current_message,
        context=context,
        system_prompt=system_prompt,
        model=model,
        max_tokens=max_tokens,
        enable_thinking=enable_thinking,
        thinking_budget=thinking_budget if enable_thinking else None,
        session_id=session_id,
        agent_id=agent_id,
        investigation_id=investigation_id,
    )


def _sync_claude_raw(
    claude_service,
    *,
    messages: List[Dict],
    model: str,
    max_tokens: int,
    enable_thinking: bool,
    thinking_budget: int,
    tools: Optional[List[Dict]],
    temperature: Optional[float],
    investigation_id: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> Any:
    """Make a direct client.messages.create() call for multi-turn tool loops."""
    import time as _time

    kwargs: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools
    if temperature is not None:
        kwargs["temperature"] = temperature
    if enable_thinking and thinking_budget:
        kwargs["thinking"] = {
            "type": "enabled",
            "budget_tokens": thinking_budget,
        }

    _raw_started = _time.monotonic()
    response = claude_service.client.messages.create(**kwargs)
    _raw_duration_ms = int((_time.monotonic() - _raw_started) * 1000)

    # Persist reasoning trace (GH #79)
    try:
        _usage = getattr(response, "usage", None)
        claude_service._persist_interaction(
            session_id=None,
            agent_id=agent_id,
            investigation_id=investigation_id,
            model=getattr(response, "model", model),
            system_prompt=None,
            request_messages=messages,
            response_content=list(response.content) if response.content else [],
            thinking_enabled=bool(enable_thinking),
            thinking_budget=thinking_budget if enable_thinking else None,
            stop_reason=getattr(response, "stop_reason", None),
            input_tokens=getattr(_usage, "input_tokens", 0) if _usage else 0,
            output_tokens=getattr(_usage, "output_tokens", 0) if _usage else 0,
            cache_read_tokens=(
                getattr(_usage, "cache_read_input_tokens", 0) if _usage else 0
            ),
            cache_creation_tokens=(
                getattr(_usage, "cache_creation_input_tokens", 0) if _usage else 0
            ),
            duration_ms=_raw_duration_ms,
        )
    except Exception as _pe:
        logger.debug(f"Reasoning-trace persist skipped (raw): {_pe}")

    return response


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _extract_result(response: Any) -> Dict[str, Any]:
    """Normalise ClaudeService.chat() output to a serialisable dict."""
    if response is None:
        return {"content": "", "type": "error", "error": "Empty response"}
    if isinstance(response, str):
        return {"content": response, "type": "text"}
    if isinstance(response, list):
        return {"content": response, "type": "blocks"}
    if isinstance(response, dict):
        return response
    return {"content": str(response), "type": "text"}


def _serialize_raw_response(response: Any) -> Dict[str, Any]:
    """Convert an Anthropic Message object into a JSON-safe dict."""
    try:
        content_blocks = []
        for block in response.content:
            if block.type == "text":
                content_blocks.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )
            elif block.type == "thinking":
                thinking_block = {"type": "thinking", "thinking": block.thinking}
                if hasattr(block, "signature") and block.signature:
                    thinking_block["signature"] = block.signature
                content_blocks.append(thinking_block)

        return {
            "content": content_blocks,
            "stop_reason": response.stop_reason,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
    except Exception as e:
        logger.error(f"Failed to serialise raw response: {e}")
        return {
            "content": [],
            "stop_reason": "error",
            "input_tokens": 0,
            "output_tokens": 0,
            "error": str(e),
        }


# ---------------------------------------------------------------------------
# Worker startup / shutdown
# ---------------------------------------------------------------------------


async def on_startup(ctx: Dict[str, Any]):
    """Initialise shared resources when the ARQ worker boots."""
    # Initialize OTEL telemetry (replaces basicConfig with structured JSON logging)
    try:
        from core.telemetry import init_telemetry

        init_telemetry("vigil-llm-worker")
    except Exception as _tel_err:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )
        logger.warning("Telemetry init failed (non-fatal): %s", _tel_err)

    from services.claude_service import ClaudeService

    claude_service = ClaudeService(
        use_backend_tools=True,
        use_mcp_tools=True,
        use_agent_sdk=False,
        enable_thinking=True,
        thinking_budget=8000,
    )
    ctx["claude_service"] = claude_service
    ctx["rate_limiter"] = asyncio.Semaphore(MAX_CONCURRENT_LLM_CALLS)
    ctx["session_store"] = RedisSessionStore(ctx["redis"])

    logger.info(f"LLM worker started (max_concurrent={MAX_CONCURRENT_LLM_CALLS})")


async def on_shutdown(ctx: Dict[str, Any]):
    logger.info("LLM worker shutting down")


# ---------------------------------------------------------------------------
# ARQ WorkerSettings
# ---------------------------------------------------------------------------


class WorkerSettings:
    """ARQ worker configuration.

    Queues are listed in priority order -- ARQ polls them left-to-right,
    so ``triage`` jobs are always consumed before ``investigation``, which
    are consumed before ``chat``.
    """

    functions = [llm_call, llm_call_raw]
    redis_settings = _redis_settings()
    queue_name = QUEUE_NAME
    max_jobs = MAX_CONCURRENT_LLM_CALLS
    job_timeout = 180
    retry_jobs = True
    max_tries = 3
    on_startup = on_startup
    on_shutdown = on_shutdown
