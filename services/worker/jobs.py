# The ARQ jobs behind LLMGateway, run by `python -m services.worker`. Every call
# passes the rate-limiter semaphore, so concurrent callers cannot exceed the cap.

import asyncio
import logging
from typing import Any, Dict, List, Optional

from core.config import get_settings
from core.llm.gateway.gateway import (
    QUEUE_NAME,
    RedisSessionStore,
)
from core.llm.gateway.gateway import redis_settings as gateway_redis_settings

logger = logging.getLogger(__name__)

MAX_CONCURRENT_LLM_CALLS = get_settings().llm_max_concurrent


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
    provider_id: Optional[str] = None,
) -> Dict[str, Any]:
    # The primary job: session load, dispatch, session save. provider_id=None
    # keeps the pre-#88 ClaudeService.chat() path exactly.
    in_flight: asyncio.Semaphore = ctx["in_flight"]
    claude_service = ctx["claude_service"]
    session_store: RedisSessionStore = ctx["session_store"]

    # Restore parent span context propagated across the ARQ/Redis boundary
    try:
        from opentelemetry.trace import SpanKind

        from core.telemetry import extract_traceparent, get_tracer

        parent_ctx = extract_traceparent({"traceparent": traceparent})
        _tracer = get_tracer("vigil.services.worker.jobs")
        worker_span = _tracer.start_span(
            "llm_worker.execute",
            context=parent_ctx,
            kind=SpanKind.CONSUMER,
        )
        worker_span.set_attribute("gen_ai.system", "anthropic")
        worker_span.set_attribute("gen_ai.request.model", model)
    except Exception:
        worker_span = None

    try:
        # Load session history if applicable
        if session_id:
            history = await session_store.load(session_id)
            if history:
                messages = history + messages

        # Multi-provider routing (GH #88): if a non-default provider_id is set
        # and the router wants the Bifrost path, dispatch there instead of
        # hitting ClaudeService directly. provider_id=None preserves the
        # pre-#88 Anthropic-SDK path exactly.
        router_result = await _maybe_dispatch_via_router(
            ctx,
            provider_id=provider_id,
            messages=messages,
            system_prompt=system_prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            enable_thinking=enable_thinking,
            thinking_budget=thinking_budget,
        )
        if router_result is not None:
            result = router_result
        else:
            await in_flight.acquire()
            try:
                response = await asyncio.to_thread(
                    _sync_claude_call,
                    claude_service,
                    messages=messages,
                    model=model,
                    max_tokens=max_tokens,
                    system_prompt=system_prompt,
                    session_id=session_id,
                    agent_id=agent_id,
                    investigation_id=investigation_id,
                )
            finally:
                in_flight.release()
            result = _extract_result(response)

        if worker_span is not None:
            try:
                worker_span.end()
            except Exception:
                pass

        # Persist session
        if session_id:
            # Bifrost results are always dicts; the legacy ClaudeService path
            # can be a bare string, so guard against it.
            assistant_content = (
                result.get("content", "") if isinstance(result, dict) else result
            )
            updated = messages + [{"role": "assistant", "content": assistant_content}]
            await session_store.save(session_id, updated)

        return result

    except Exception as exc:
        if worker_span is not None:
            try:
                worker_span.end()
            except Exception:
                pass
        error_msg = f"{type(exc).__name__}: {exc}"
        logger.error("llm_call failed (returning error dict): %s", error_msg)
        return {"content": "", "type": "error", "error": error_msg}


async def _maybe_dispatch_via_router(
    ctx: Dict[str, Any],
    *,
    provider_id: Optional[str],
    messages: List[Dict],
    system_prompt: Optional[str],
    model: str,
    max_tokens: int,
    temperature: Optional[float],
    tools: Optional[List[Dict]],
    enable_thinking: bool,
    thinking_budget: int,
) -> Optional[Dict[str, Any]]:
    # Returns None when the caller should fall back to ClaudeService. Everything
    # reaches Bifrost either way; the fallback just keeps ClaudeService's tool loop.
    if provider_id is None:
        return None

    router = ctx.get("llm_router")
    if router is None:
        logger.debug("llm_router not initialized; falling back to ClaudeService")
        return None

    try:
        from core.llm.router.router import get_provider_spec

        spec = get_provider_spec(provider_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to resolve provider %s: %s", provider_id, exc)
        return None

    if spec is None:
        logger.warning(
            "Provider %s not found; falling back to ClaudeService", provider_id
        )
        return None

    # The fallback this used to take was ClaudeService's tool loop, context
    # reduction and session management. None of the three exist (#629, #631,
    # #632), so every provider dispatches the one way.
    in_flight: asyncio.Semaphore = ctx["in_flight"]
    await in_flight.acquire()
    try:
        return await router.dispatch(
            provider=spec,
            messages=messages,
            system_prompt=system_prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            enable_thinking=enable_thinking,
            thinking_budget=thinking_budget,
        )
    finally:
        in_flight.release()


# The two _sync_* helpers below run inside asyncio.to_thread.


def _sync_claude_call(
    claude_service,
    *,
    messages: List[Dict],
    model: str,
    max_tokens: int,
    system_prompt: Optional[str],
    session_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    investigation_id: Optional[str] = None,
) -> Any:
    current_message = messages[-1]["content"] if messages else ""
    context = messages[:-1] if len(messages) > 1 else None

    return claude_service.chat(
        message=current_message,
        context=context,
        system_prompt=system_prompt,
        model=model,
        max_tokens=max_tokens,
        session_id=session_id,
        agent_id=agent_id,
        investigation_id=investigation_id,
    )


def _extract_result(response: Any) -> Dict[str, Any]:
    # Normalise ClaudeService.chat() output to a serialisable dict.
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
    # Convert an Anthropic Message object into a JSON-safe dict.
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
            # #184 Phase 3: surface cache tokens so the daemon can price
            # them at provider-specific rates instead of full input rate.
            "cache_read_tokens": getattr(response.usage, "cache_read_input_tokens", 0),
            "cache_creation_tokens": getattr(
                response.usage, "cache_creation_input_tokens", 0
            ),
            # This path is only taken for the shared (default Anthropic)
            # ClaudeService; make the provider explicit so cost accounting
            # doesn't rely on a downstream ``or "anthropic"`` fallback.
            "provider": "anthropic",
        }
    except Exception as e:
        logger.error(f"Failed to serialise raw response: {e}")
        return {
            "content": [],
            "stop_reason": "error",
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "error": str(e),
        }


async def on_startup(ctx: Dict[str, Any]):
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

    # Initialize the SQLAlchemy DB manager so downstream code (skill tool
    # loading, reasoning-trace persistence, provider-key resolution) can
    # query the DB. The backend process does this in its FastAPI startup
    # hook; the worker is a separate process and must do it itself.
    try:
        from core.storage.connection import get_db_manager

        db_manager = get_db_manager()
        if db_manager._engine is None:
            db_manager.initialize()
            logger.info("LLM worker: DB manager initialized")
    except Exception as _db_err:
        logger.warning(
            "LLM worker DB init failed (skill tools + reasoning traces will be disabled): %s",
            _db_err,
        )

    from core.llm.harness.claude import ClaudeService

    claude_service = ClaudeService()
    ctx["claude_service"] = claude_service
    # A cap on calls in flight, not a rate limit: the rate is Bifrost's, and
    # how a client answers its refusals is core.llm.gateway_retry's.
    ctx["in_flight"] = asyncio.Semaphore(MAX_CONCURRENT_LLM_CALLS)
    ctx["session_store"] = RedisSessionStore(ctx["redis"])

    # Multi-provider routing (GH #88). Router is optional: if construction
    # fails (e.g. openai not installed), worker continues in Anthropic-only
    # mode and provider_id kwargs are silently ignored.
    try:
        from core.llm.router.router import LLMRouter

        ctx["llm_router"] = LLMRouter()
        logger.info(
            "LLM router initialized (Bifrost URL=%s)", ctx["llm_router"].bifrost_url
        )
    except Exception as _router_err:
        ctx["llm_router"] = None
        logger.warning("LLM router init skipped (non-fatal): %s", _router_err)

    logger.info(f"LLM worker started (max_concurrent={MAX_CONCURRENT_LLM_CALLS})")


async def on_shutdown(ctx: Dict[str, Any]):
    logger.info("LLM worker shutting down")


class WorkerSettings:
    # ARQ polls queues left-to-right, so triage is always consumed before
    # investigation, and investigation before chat.
    functions = [llm_call]
    # ARQ reads this attribute by name; alias the import so the class
    # attribute does not shadow the function producing it.
    redis_settings = gateway_redis_settings()
    queue_name = QUEUE_NAME
    max_jobs = MAX_CONCURRENT_LLM_CALLS
    job_timeout = 180
    retry_jobs = True
    max_tries = 3
    on_startup = on_startup
    on_shutdown = on_shutdown
