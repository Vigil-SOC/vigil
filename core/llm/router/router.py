from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Tuple

from core.config import get_settings
from core.llm.router.format import (
    anthropic_messages_to_openai,
    anthropic_tools_to_openai,
)
from core.llm.security import (
    PromptInjectionBlocked,
    scan_for_injection,
    wrap_tool_result,
)
from core.secrets import get_secret

logger = logging.getLogger(__name__)

DispatchPath = Literal["bifrost"]


# Kept as the name the dispatch paths call. The policy is shared with the agent
# worker rather than restated here: a 429 is retried, a 402 is terminal. A
# factory rather than a coroutine, because a retry needs a fresh one.
async def _wrap_budget_errors(call):
    from core.llm.gateway_retry import through_gateway

    return await through_gateway(call)


@dataclass(frozen=True)
class ProviderSpec:
    """Minimal view of a row from llm_provider_configs.

    Kept as a plain dataclass (not the ORM model) so this module doesn't
    import the SQLAlchemy session into the worker hot path.
    """

    provider_id: str
    provider_type: str
    base_url: Optional[str]
    api_key_ref: Optional[str]
    default_model: str
    config: Dict[str, Any]


def _bifrost_url() -> str:
    return get_settings().bifrost_url.rstrip("/")


def _block_on_injection() -> bool:
    return get_settings().prompt_injection_block


def _normalize_openai_tool_calls(tool_calls: Any) -> Optional[List[Dict[str, Any]]]:
    if not tool_calls:
        return None
    normalized: List[Dict[str, Any]] = []
    for tc in tool_calls:
        fn = getattr(tc, "function", None)
        name = getattr(fn, "name", None) if fn is not None else None
        raw_args = getattr(fn, "arguments", None) if fn is not None else None
        try:
            parsed = json.loads(raw_args) if raw_args else {}
        except (json.JSONDecodeError, TypeError):
            parsed = {}
        normalized.append(
            {
                "id": getattr(tc, "id", None),
                "name": name,
                "input": parsed if isinstance(parsed, dict) else {},
            }
        )
    return normalized or None


def _wrap_tool_results_in_messages(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Walk Anthropic-shape messages and wrap every ``tool_result`` block.

    The wrapper is idempotent (see ``core.llm.security.wrap_tool_result``)
    so messages that already passed through ``ClaudeService`` won't be
    double-wrapped here.
    """

    out: List[Dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue
        new_content: List[Any] = []
        rewritten = False
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                new_content.append(block)
                continue
            tool_use_id = block.get("tool_use_id")
            inner = block.get("content")
            wrapped_inner: Any
            if isinstance(inner, str):
                wrapped_inner = wrap_tool_result(
                    inner, source="router", tool=str(tool_use_id or "unknown")
                )
            elif isinstance(inner, list):
                wrapped_inner = []
                for sub in inner:
                    if (
                        isinstance(sub, dict)
                        and sub.get("type") == "text"
                        and isinstance(sub.get("text"), str)
                    ):
                        wrapped_inner.append(
                            {
                                **sub,
                                "text": wrap_tool_result(
                                    sub["text"],
                                    source="router",
                                    tool=str(tool_use_id or "unknown"),
                                ),
                            }
                        )
                    else:
                        wrapped_inner.append(sub)
            else:
                wrapped_inner = inner
            new_content.append({**block, "content": wrapped_inner})
            rewritten = True
        out.append({**msg, "content": new_content} if rewritten else msg)
    return out


def _scan_messages_for_injection(messages: List[Dict[str, Any]]) -> List[str]:
    """Run pattern scan over text content in *messages*; return matched names."""

    patterns: List[str] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            patterns.extend(scan_for_injection(content).patterns)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    patterns.extend(scan_for_injection(block["text"]).patterns)
    return patterns


def _bifrost_headers(interaction_id: Optional[str] = None) -> Dict[str, str]:
    """Log-correlation and budget-VK headers every Bifrost call carries."""
    headers: Dict[str, str] = {}
    if interaction_id:
        headers["x-bf-lh-vigil-interaction-id"] = interaction_id
    try:
        from core.llm.cost.budget import get_active_vk, should_enforce

        if should_enforce():
            vk = get_active_vk()
            if vk:
                headers["x-bf-vk"] = vk
    except Exception as exc:
        logger.debug("budget_service unavailable (%s); proceeding without x-bf-vk", exc)
    return headers


def _pre_dispatch_sanitize(
    messages: List[Dict[str, Any]],
    system_prompt: Optional[str],
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Issue #87 chokepoint: wrap tool_results, log injection patterns,
    optionally block when ``PROMPT_INJECTION_BLOCK=true``.

    Returns the (possibly rewritten) ``messages`` and the system prompt
    (returned as-is — we never silently mutate user system prompts).
    """

    wrapped = _wrap_tool_results_in_messages(messages)

    msg_patterns = _scan_messages_for_injection(messages)
    sys_patterns = scan_for_injection(system_prompt).patterns

    if msg_patterns or sys_patterns:
        logger.info(
            "prompt_injection scan",
            extra={
                "event": "prompt_injection.scan",
                "message_patterns": msg_patterns,
                "system_prompt_patterns": sys_patterns,
                "block_mode": _block_on_injection(),
            },
        )
        if _block_on_injection():
            raise PromptInjectionBlocked(msg_patterns + sys_patterns)

    return wrapped, system_prompt


class LLMRouter:
    """Dispatches chat completions through the Bifrost gateway.

    The router does NOT own the DB session. Callers construct a
    ``ProviderSpec`` from an ``LLMProviderConfig`` row (via
    ``provider_spec_from_row``) and pass it in. This keeps the worker
    hot path free of DB imports and makes unit-testing trivial.
    """

    def __init__(self, bifrost_url: Optional[str] = None):
        self.bifrost_url = (bifrost_url or _bifrost_url()).rstrip("/")

    # ---- path selection --------------------------------------------------

    # ---- dispatch --------------------------------------------------------

    async def dispatch(
        self,
        *,
        provider: ProviderSpec,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        enable_thinking: bool = False,
        thinking_budget: int = 10000,
        interaction_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send a chat completion via Bifrost.

        Anthropic calls hit Bifrost's ``/anthropic`` passthrough so
        extended thinking and native prompt caching round-trip intact.
        Other providers use Bifrost's OpenAI-format ``/v1`` endpoint.

        ``interaction_id`` (when set) is attached as the
        ``x-bf-lh-vigil-interaction-id`` header — Bifrost's logging plugin
        captures any ``x-bf-lh-*`` header into ``LogEntry.metadata``, so
        operators can correlate Vigil's local ``LLMInteractionLog`` row
        with the matching Bifrost log entry by that UUID. (#185)
        """
        messages, system_prompt = _pre_dispatch_sanitize(messages, system_prompt)
        model = model or provider.default_model

        extra_headers = _bifrost_headers(interaction_id)
        # Convert empty dict back to None so the dispatch helpers can use a
        # truthy check for "should I send any extra headers" without leaking
        # an empty dict into the SDK call.
        extra_headers_or_none: Optional[Dict[str, str]] = (
            extra_headers if extra_headers else None
        )
        # One schema out (ADR 0011). Anthropic used to take Bifrost's /anthropic
        # passthrough to keep extended thinking and cache_control; both were
        # traded away, and by #632 nothing asked for either.
        return await _wrap_budget_errors(
            lambda: self._dispatch_bifrost_openai(
                provider=provider,
                messages=messages,
                system_prompt=system_prompt,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=tools,
                enable_thinking=enable_thinking,
                extra_headers=extra_headers_or_none,
            )
        )

    # ---- backends --------------------------------------------------------

    async def _dispatch_bifrost_openai(
        self,
        *,
        provider: ProviderSpec,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str],
        model: str,
        max_tokens: int,
        temperature: Optional[float],
        tools: Optional[List[Dict[str, Any]]],
        enable_thinking: bool = False,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        from openai import AsyncOpenAI  # lazy — avoids hard dep for tests

        # Callers (the daemon tool loop, workflows) build conversations in
        # Anthropic shape — assistant tool_use blocks, user tool_result blocks,
        # and tools with `input_schema`. Translate both to OpenAI shape so the
        # multi-turn tool loop round-trips; string-content messages and already
        # OpenAI-shaped tools pass through untouched.
        oai_messages: List[Dict[str, Any]] = []
        if system_prompt:
            oai_messages.append({"role": "system", "content": system_prompt})
        oai_messages.extend(anthropic_messages_to_openai(messages))

        client = AsyncOpenAI(
            base_url=f"{self.bifrost_url}/v1",
            api_key="bifrost",  # Bifrost ignores this; per-provider keys are in its config
        )
        kwargs: Dict[str, Any] = {
            "model": f"{provider.provider_type}/{model}",
            "messages": oai_messages,
            "max_tokens": max_tokens,
        }
        # Ollama's OpenAI-compatible API uses ``reasoning_effort`` rather
        # than its native ``think`` field. When the option is omitted,
        # reasoning-capable local models may enable it implicitly and consume
        # the response budget before producing user-visible content. Preserve
        # the caller's explicit thinking choice across the Bifrost boundary.
        if provider.provider_type == "ollama":
            kwargs["reasoning_effort"] = "medium" if enable_thinking else "none"
        if temperature is not None:
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = anthropic_tools_to_openai(tools)
        if extra_headers:
            kwargs["extra_headers"] = extra_headers

        try:
            resp = await client.chat.completions.create(**kwargs)
            choice = resp.choices[0].message
            usage = getattr(resp, "usage", None)
            # OpenAI exposes prompt-cache hits via usage.prompt_tokens_details.cached_tokens.
            # OpenAI bills cached tokens at a discounted rate but doesn't bill a
            # separate "cache creation" tier the way Anthropic does — so we
            # populate cache_read_tokens and leave cache_creation_tokens at 0.
            # Without this extraction the cost-per-call math under-credits OpenAI
            # cache hits (full input rate instead of the discounted cache rate),
            # which is the asymmetry #184 acceptance #2 calls out.
            cache_read = 0
            if usage is not None:
                details = getattr(usage, "prompt_tokens_details", None)
                if details is not None:
                    cache_read = getattr(details, "cached_tokens", 0) or 0
            return {
                "content": choice.content or "",
                # Normalize OpenAI tool-call objects to {id, name, input} dicts
                # so _adapt_router_result_to_raw can build Anthropic tool_use
                # blocks without touching SDK-object internals. Arguments arrive
                # as a JSON string; parse to a dict (empty on malformed JSON).
                "tool_calls": _normalize_openai_tool_calls(
                    getattr(choice, "tool_calls", None)
                ),
                "model": resp.model,
                "input_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
                "output_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
                "cache_read_tokens": cache_read,
                "cache_creation_tokens": 0,
                "provider": provider.provider_type,
                "path": "bifrost",
            }
        finally:
            # AsyncOpenAI holds an httpx connection pool; close it so file
            # descriptors / connections don't leak per call (chat()'s
            # non-streaming path routes through here).
            await client.close()

    async def stream_openai_raw(
        self,
        *,
        provider: ProviderSpec,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        interaction_id: Optional[str] = None,
        include_usage: bool = False,
        enable_thinking: bool = False,
    ):
        """Yield raw OpenAI stream chunks (tool-call deltas, finish_reason,
        usage) for non-Anthropic Bifrost providers."""
        from openai import AsyncOpenAI

        messages, system_prompt = _pre_dispatch_sanitize(messages, system_prompt)
        model = model or provider.default_model

        oai_messages: List[Dict[str, Any]] = []
        if system_prompt:
            oai_messages.append({"role": "system", "content": system_prompt})
        oai_messages.extend(anthropic_messages_to_openai(messages))

        client = AsyncOpenAI(
            base_url=f"{self.bifrost_url}/v1",
            api_key="bifrost",
        )
        kwargs: Dict[str, Any] = {
            "model": f"{provider.provider_type}/{model}",
            "messages": oai_messages,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if provider.provider_type == "ollama":
            kwargs["reasoning_effort"] = "medium" if enable_thinking else "none"
        if include_usage:
            kwargs["stream_options"] = {"include_usage": True}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = anthropic_tools_to_openai(tools)
        extra_headers = _bifrost_headers(interaction_id)
        if extra_headers:
            kwargs["extra_headers"] = extra_headers

        try:
            stream = await client.chat.completions.create(**kwargs)
            async for chunk in stream:
                yield chunk
        except Exception as exc:
            # The stream already ran, so there is nothing to retry: read the
            # status and re-raise, rather than attempt the call again.
            from core.llm.gateway_retry import translate

            raise translate(exc) from exc
        finally:
            # AsyncOpenAI holds an httpx connection pool; close it on normal
            # completion, error, AND consumer disconnect (GeneratorExit, e.g.
            # the SSE client goes away mid-stream) so file descriptors /
            # connections don't accumulate under load.
            await client.close()

    async def dispatch_openai_stream(
        self,
        *,
        provider: ProviderSpec,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        interaction_id: Optional[str] = None,
        enable_thinking: bool = False,
    ):
        """Yield OpenAI-format text chunks for non-Anthropic Bifrost providers."""
        async for chunk in self.stream_openai_raw(
            provider=provider,
            messages=messages,
            system_prompt=system_prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            interaction_id=interaction_id,
            enable_thinking=enable_thinking,
        ):
            if not chunk.choices:
                continue
            content = getattr(chunk.choices[0].delta, "content", None)
            if content:
                yield {"type": "text", "content": content}


# ---------------------------------------------------------------------------
# DB-facing helpers — importable without circular deps
# ---------------------------------------------------------------------------


def provider_spec_from_row(row) -> ProviderSpec:
    """Convert an LLMProviderConfig ORM row into a ProviderSpec."""
    return ProviderSpec(
        provider_id=row.provider_id,
        provider_type=row.provider_type,
        base_url=row.base_url,
        api_key_ref=row.api_key_ref,
        default_model=row.default_model,
        config=dict(row.config or {}),
    )


def get_provider_spec(provider_id: Optional[str]) -> Optional[ProviderSpec]:
    """One provider's dispatch spec, or None when it cannot be dispatched to.

    Inactive rows are refused, matching ``get_default_provider_spec``. A saved
    ``ai_model_configs`` assignment outlives the row it names — the FK is ON
    DELETE RESTRICT, so retiring a provider deactivates it rather than deleting
    it — and without this an assignment kept dispatching to a provider whose
    credential the gateway no longer has. The caller then falls back to the
    configured default, as it does for an assignment naming no row at all.
    """
    try:
        from core.storage.connection import get_db_session
        from core.storage.models import LLMProviderConfig
    except Exception as exc:
        logger.debug("provider spec DB lookup skipped: %s", exc)
        return None

    session = get_db_session()
    try:
        if provider_id:
            row = session.get(LLMProviderConfig, provider_id)
            if row is not None and not row.is_active:
                logger.info("Provider %s is inactive — not dispatchable", provider_id)
                row = None
        else:
            row = (
                session.query(LLMProviderConfig)
                .filter(
                    LLMProviderConfig.provider_type == "anthropic",
                    LLMProviderConfig.is_default.is_(True),
                    LLMProviderConfig.is_active.is_(True),
                )
                .first()
            )
        if row is None:
            return None
        return provider_spec_from_row(row)
    finally:
        session.close()


def get_default_provider_spec() -> Optional[ProviderSpec]:
    try:
        from core.storage.connection import get_db_session
        from core.storage.models import LLMProviderConfig
    except Exception as exc:  # noqa: BLE001
        logger.debug("default provider spec DB lookup skipped: %s", exc)
        return None

    session = get_db_session()
    try:
        # Single-default is enforced per provider_type, so multiple rows can
        # carry is_default=True across types (e.g. an Anthropic default AND an
        # Ollama default). Order by created_at so the pick is stable across
        # runs/DBs instead of relying on undefined SQL row order.
        row = (
            session.query(LLMProviderConfig)
            .filter(
                LLMProviderConfig.is_active.is_(True),
                LLMProviderConfig.is_default.is_(True),
            )
            .order_by(LLMProviderConfig.created_at)
            .first()
        )
        if row is None:
            row = (
                session.query(LLMProviderConfig)
                .filter(LLMProviderConfig.is_active.is_(True))
                .order_by(LLMProviderConfig.created_at)
                .first()
            )
        if row is None:
            return None
        return provider_spec_from_row(row)
    except Exception as exc:  # noqa: BLE001
        logger.debug("default provider spec lookup failed: %s", exc)
        return None
    finally:
        session.close()


def discover_anthropic_api_key() -> Optional[str]:
    """Resolve an Anthropic API key from the UI-saved provider rows.

    Looks up rows in ``llm_provider_configs`` (the table populated by the
    Settings → AI / LLM Providers UI) and resolves the secret each row
    points at via ``api_key_ref``. Preference order:

    1. The default Anthropic provider row.
    2. Any active Anthropic row that has an ``api_key_ref`` set.

    Returns ``None`` if the DB is unreachable, no Anthropic provider has
    been configured via the UI, or the referenced secret is missing.

    Callers should use this as a fallback after the legacy
    ``CLAUDE_API_KEY`` / ``ANTHROPIC_API_KEY`` chain. The point is that
    a user who configured Anthropic via the UI shouldn't get a
    ``"Claude API not configured"`` error from chat endpoints just
    because those endpoints went through ``ClaudeService`` instead of
    ``LLMRouter``.
    """
    if get_secret is None:
        return None
    try:
        from core.storage.connection import get_db_session
        from core.storage.models import LLMProviderConfig
    except Exception as exc:  # noqa: BLE001
        logger.debug("anthropic key discovery: DB unavailable (%s)", exc)
        return None

    session = get_db_session()
    try:
        # Default-active first, then any active row with a key ref.
        candidates = (
            session.query(LLMProviderConfig)
            .filter(
                LLMProviderConfig.provider_type == "anthropic",
                LLMProviderConfig.is_active.is_(True),
                LLMProviderConfig.api_key_ref.isnot(None),
            )
            .order_by(LLMProviderConfig.is_default.desc())
            .all()
        )
        for row in candidates:
            value = get_secret(row.api_key_ref)
            if value:
                logger.debug(
                    "Resolved Anthropic API key from provider row %s",
                    row.provider_id,
                )
                return value
        return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("anthropic key discovery failed: %s", exc)
        return None
    finally:
        session.close()
