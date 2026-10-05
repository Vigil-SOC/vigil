# The ARQ jobs behind LLMGateway, run by `python -m services.worker`. Every call
# passes the rate-limiter semaphore, so concurrent callers cannot exceed the cap.

import asyncio
import logging
import socket
from typing import Any, Dict, Optional

from core.config import get_settings
from core.llm.bifrost.admin import refresh_gateway_rates, run_gateway_rates_refresher
from core.llm.gateway.gateway import QUEUE_NAME
from core.llm.gateway.gateway import redis_settings as gateway_redis_settings
from core.telemetry import configure_logging, init_telemetry

logger = logging.getLogger(__name__)

MAX_CONCURRENT_LLM_CALLS = get_settings().llm_max_concurrent


async def llm_call(
    ctx: Dict[str, Any],
    prompt: str,
    model: str,
    max_tokens: int,
    temperature: Optional[float],
    traceparent: str = "",
    provider_id: Optional[str] = None,
) -> Dict[str, Any]:
    # One stateless completion. provider_id=None keeps the pre-#88
    # ClaudeService.chat() path exactly.
    in_flight: asyncio.Semaphore = ctx["in_flight"]
    claude_service = ctx["claude_service"]

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
        # Multi-provider routing (GH #88): if a non-default provider_id is set
        # and the router wants the Bifrost path, dispatch there instead of
        # hitting ClaudeService directly. provider_id=None preserves the
        # pre-#88 Anthropic-SDK path exactly.
        result = await _maybe_dispatch_via_router(
            ctx,
            provider_id=provider_id,
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if result is None:
            await in_flight.acquire()
            try:
                response = await asyncio.to_thread(
                    claude_service.chat,
                    message=prompt,
                    model=model,
                    max_tokens=max_tokens,
                )
            finally:
                in_flight.release()
            result = (
                {"content": response, "type": "text"}
                if response is not None
                else {"content": "", "type": "error", "error": "Empty response"}
            )

        if worker_span is not None:
            try:
                worker_span.end()
            except Exception:
                pass

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
    prompt: str,
    model: str,
    max_tokens: int,
    temperature: Optional[float],
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
            messages=[{"role": "user", "content": prompt}],
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    finally:
        in_flight.release()


async def on_startup(ctx: Dict[str, Any]):
    configure_logging("INFO")
    try:
        init_telemetry("vigil-llm-worker")
    except Exception as _tel_err:
        logger.warning("Telemetry init failed (non-fatal): %s", _tel_err)

    # Initialize the SQLAlchemy DB manager so downstream code (reasoning-trace
    # persistence, provider-key resolution) can query the DB. The backend process does this in its FastAPI startup
    # hook; the worker is a separate process and must do it itself.
    try:
        from core.storage.connection import get_db_manager

        db_manager = get_db_manager()
        if db_manager._engine is None:
            db_manager.initialize()
            logger.info("LLM worker: DB manager initialized")
    except Exception as _db_err:
        logger.warning(
            "LLM worker DB init failed (reasoning traces will be disabled): %s",
            _db_err,
        )

    from core.llm.harness.claude import ClaudeService

    claude_service = ClaudeService()
    ctx["claude_service"] = claude_service
    # This process prices every call it makes, from its own copy of the
    # gateway's rates; without it each one would record as unpriced.
    await refresh_gateway_rates()
    ctx["rates_refresher"] = asyncio.create_task(run_gateway_rates_refresher())
    # A cap on calls in flight, not a rate limit: the rate is Bifrost's, and
    # how a client answers its refusals is core.llm.gateway_retry's.
    ctx["in_flight"] = asyncio.Semaphore(MAX_CONCURRENT_LLM_CALLS)

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
    refresher = ctx.get("rates_refresher")
    if refresher is not None:
        refresher.cancel()


class WorkerSettings:
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
    # The Helm liveness probe runs `arq --check` against this key. ARQ's default
    # interval is 3600 s, which would leave a wedged worker undetected for an
    # hour; the key now lives health_check_interval + 1 s and is rewritten from
    # the poll loop, so it lapses ~31 s after the loop stops turning.
    health_check_interval = 30
    # Per pod, not ARQ's per-queue default: replicas share the queue, so with a
    # shared key one healthy replica would vouch for a wedged one, and any
    # replica's clean shutdown (Worker.close deletes the key) would fail every
    # other replica's probe. The probe execs in the same container, so it
    # resolves the same hostname.
    health_check_key = f"{QUEUE_NAME}:health-check:{socket.gethostname()}"
