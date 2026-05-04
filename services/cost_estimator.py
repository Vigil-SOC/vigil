"""Pre-call LLM cost estimation (#184 Phase 2).

Lets callers (chat composer, daemon planner, workflow engine) ask
"approximately what will this prompt cost on this model?" before
spending money. Returns a low/high USD band rather than a point estimate
because output length is unknown — the high bound assumes ``max_tokens``
output, the low bound assumes a no-output completion (e.g. an
immediately-stopped tool call).

Token counting strategy by provider:

  - **Anthropic**: uses the SDK's free ``client.messages.count_tokens()``
    endpoint, routed through Bifrost like every other Anthropic call.
    Returns exact prompt-token counts including system prompt and tools.

  - **OpenAI**: uses ``tiktoken`` if available (encoder lookup by model
    name), else falls back to a 4-chars-per-token heuristic. The heuristic
    is good enough for budget gating but not for billing — callers see
    ``pricing_source="heuristic"`` and can badge the estimate accordingly.

  - **Ollama / unknown**: char heuristic + ``$0`` rates → returns ``$0``.
    Self-hosted compute cost is out of scope (#184 explicitly defers it).

Cache hits are not modeled in v1 — the estimator is for the cold-path
"what will this cost if nothing's cached" question. Once the call lands,
the actual cost (cache-aware via ``compute_call_cost``) will typically be
lower than the high bound for cache-friendly workloads. That asymmetry is
fine: estimates over-bound, actuals are exact.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CostEstimate:
    """Result of a pre-call cost estimate.

    ``pricing_source`` propagates from the model registry plus a token-
    counting flag — callers can show "approximate" badges when this is
    anything other than ``"exact"``.
    """

    provider_type: str
    model_id: str
    input_tokens: int
    output_tokens_max: int
    low_usd: float
    high_usd: float
    pricing_source: str  # "exact" | "heuristic" | "zero" | "unknown"
    token_count_method: str  # "anthropic_count_tokens" | "tiktoken" | "char_heuristic"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------


_CHARS_PER_TOKEN_HEURISTIC = 4
"""Rough English-text token density. Conservative for code (which packs
denser) and for non-Latin scripts (which pack looser); good enough for
budget gating, not good enough for billing."""


def _flatten_message_text(messages: List[Dict[str, Any]]) -> str:
    """Concatenate the text portion of every message into one string.

    The estimator only needs character count, not structured content, so
    multimodal blocks (images, tool_use, tool_result) are ignored — they
    have their own token costs the heuristic can't capture and the
    Anthropic ``count_tokens`` API handles natively.
    """
    parts: List[str] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text") or ""
                    if text:
                        parts.append(text)
    return "\n".join(parts)


def _char_heuristic_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN_HEURISTIC)


def _count_tokens_openai(model_id: str, text: str) -> tuple[int, str]:
    """Return ``(token_count, method_used)``.

    Tries ``tiktoken`` first, falls back to the char heuristic if the
    encoder isn't installed or doesn't recognise the model. The fallback
    is logged at debug — callers see the method label and can decide
    whether to trust it.
    """
    if not text:
        return (0, "char_heuristic")
    try:
        import tiktoken  # type: ignore
    except ImportError:
        return (_char_heuristic_tokens(text), "char_heuristic")

    try:
        enc = tiktoken.encoding_for_model(model_id)
    except KeyError:
        # Unknown model — fall back to the cl100k_base encoder used by
        # all current OpenAI chat models. Not perfect for novel models
        # but better than the char heuristic.
        try:
            enc = tiktoken.get_encoding("cl100k_base")
        except Exception:  # noqa: BLE001
            return (_char_heuristic_tokens(text), "char_heuristic")
    except Exception as exc:  # noqa: BLE001
        logger.debug("tiktoken lookup for %s failed: %s", model_id, exc)
        return (_char_heuristic_tokens(text), "char_heuristic")

    return (len(enc.encode(text)), "tiktoken")


# ---------------------------------------------------------------------------
# Per-provider estimators
# ---------------------------------------------------------------------------


async def estimate_anthropic(
    *,
    model_id: str,
    messages: List[Dict[str, Any]],
    system_prompt: Optional[str] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    max_tokens: int = 4096,
) -> CostEstimate:
    """Estimate USD cost of an Anthropic call by hitting count_tokens.

    Routes through Bifrost via ``services.llm_clients.create_async_anthropic_client``
    so the count_tokens call obeys the single-routing-path policy.
    """
    from services.model_registry import get_registry

    registry = get_registry()
    in_rate, out_rate = registry.get_cost_rates(model_id, "anthropic")
    pricing_source = registry.get_pricing_source(model_id, "anthropic")

    api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY")
    input_tokens = 0
    method = "char_heuristic"

    if api_key:
        try:
            from services.llm_clients import create_async_anthropic_client

            client = create_async_anthropic_client(api_key, timeout=30.0)
            kwargs: Dict[str, Any] = {"model": model_id, "messages": messages}
            if system_prompt:
                kwargs["system"] = system_prompt
            if tools:
                kwargs["tools"] = tools
            resp = await client.messages.count_tokens(**kwargs)
            input_tokens = int(getattr(resp, "input_tokens", 0) or 0)
            method = "anthropic_count_tokens"
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "count_tokens for %s failed (%s) — falling back", model_id, exc
            )

    if method == "char_heuristic":
        # No API key or count_tokens unavailable — fall back so the
        # estimator is still useful in tests / offline contexts.
        text = _flatten_message_text(messages)
        if system_prompt:
            text = system_prompt + "\n" + text
        input_tokens = _char_heuristic_tokens(text)

    low_usd = input_tokens * in_rate
    high_usd = low_usd + max_tokens * out_rate
    return CostEstimate(
        provider_type="anthropic",
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens_max=max_tokens,
        low_usd=low_usd,
        high_usd=high_usd,
        pricing_source=pricing_source,
        token_count_method=method,
    )


def estimate_openai(
    *,
    model_id: str,
    messages: List[Dict[str, Any]],
    system_prompt: Optional[str] = None,
    max_tokens: int = 4096,
) -> CostEstimate:
    """Estimate USD cost of an OpenAI call using tiktoken (or char fallback).

    Synchronous — no network calls. Cheap to use in a hot path.
    """
    from services.model_registry import get_registry

    registry = get_registry()
    in_rate, out_rate = registry.get_cost_rates(model_id, "openai")
    pricing_source = registry.get_pricing_source(model_id, "openai")

    text = _flatten_message_text(messages)
    if system_prompt:
        text = system_prompt + "\n" + text
    input_tokens, method = _count_tokens_openai(model_id, text)

    low_usd = input_tokens * in_rate
    high_usd = low_usd + max_tokens * out_rate
    return CostEstimate(
        provider_type="openai",
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens_max=max_tokens,
        low_usd=low_usd,
        high_usd=high_usd,
        pricing_source=pricing_source,
        token_count_method=method,
    )


def estimate_ollama(
    *,
    model_id: str,
    messages: List[Dict[str, Any]],
    system_prompt: Optional[str] = None,
    max_tokens: int = 4096,
) -> CostEstimate:
    """Self-hosted → $0. Tokens estimated via char heuristic for context gating."""
    text = _flatten_message_text(messages)
    if system_prompt:
        text = system_prompt + "\n" + text
    input_tokens = _char_heuristic_tokens(text)
    return CostEstimate(
        provider_type="ollama",
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens_max=max_tokens,
        low_usd=0.0,
        high_usd=0.0,
        pricing_source="zero",
        token_count_method="char_heuristic",
    )


# ---------------------------------------------------------------------------
# Provider-agnostic facade
# ---------------------------------------------------------------------------


async def estimate_cost(
    *,
    provider_type: str,
    model_id: str,
    messages: List[Dict[str, Any]],
    system_prompt: Optional[str] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    max_tokens: int = 4096,
) -> CostEstimate:
    """Dispatch to the right provider-specific estimator.

    Unknown ``provider_type`` falls back to the OpenAI estimator's char
    heuristic with $0 rates — better than raising, since callers want a
    best-effort number even for novel providers.
    """
    if provider_type == "anthropic":
        return await estimate_anthropic(
            model_id=model_id,
            messages=messages,
            system_prompt=system_prompt,
            tools=tools,
            max_tokens=max_tokens,
        )
    if provider_type == "openai":
        return estimate_openai(
            model_id=model_id,
            messages=messages,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
        )
    if provider_type == "ollama":
        return estimate_ollama(
            model_id=model_id,
            messages=messages,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
        )

    # Genuinely unknown provider (not anthropic/openai/ollama). Surface the
    # event so dashboards see it instead of silently recording $0 — same
    # treatment we give unknown models in services.model_registry.
    logger.warning(
        "estimate_cost: unknown provider_type=%r model_id=%r — returning $0 "
        "with pricing_source='unknown'",
        provider_type,
        model_id,
    )
    try:
        from services.model_registry import _record_pricing_unknown

        _record_pricing_unknown(provider_type or "unknown", model_id or "unknown")
    except Exception:
        pass

    text = _flatten_message_text(messages)
    if system_prompt:
        text = system_prompt + "\n" + text
    input_tokens = _char_heuristic_tokens(text)
    return CostEstimate(
        provider_type=provider_type,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens_max=max_tokens,
        low_usd=0.0,
        high_usd=0.0,
        pricing_source="unknown",
        token_count_method="char_heuristic",
    )
