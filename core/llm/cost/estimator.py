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

  - **Everything else** (Vertex, Gemini, Bedrock, Azure, Ollama, ...): the
    char heuristic, priced at the registry's rates for that provider. Ollama
    resolves to ``$0`` (self-hosted compute is out of scope, #184); a pair
    the registry cannot price is ``$0`` with ``pricing_source="unknown"``.

Cache hits are not modeled in v1 — the estimator is for the cold-path
"what will this cost if nothing's cached" question. Once the call lands,
the actual cost (cache-aware via ``compute_call_cost``) will typically be
lower than the high bound for cache-friendly workloads. That asymmetry is
fine: estimates over-bound, actuals are exact.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from core.llm.providers.registry import get_registry
from core.secrets import get_secret

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


async def _count_tokens_anthropic(
    model_id: str,
    messages: List[Dict[str, Any]],
    system_prompt: Optional[str],
    tools: Optional[List[Dict[str, Any]]],
) -> Optional[int]:
    """Exact prompt tokens from Anthropic's free ``count_tokens``, or ``None``.

    Routes through Bifrost via ``core.llm.providers.clients.create_async_anthropic_client``
    so the count_tokens call obeys the single-routing-path policy.
    """
    api_key = get_secret("ANTHROPIC_API_KEY") or get_secret("CLAUDE_API_KEY")
    if not api_key:
        return None
    try:
        from core.llm.providers.clients import create_async_anthropic_client

        client = create_async_anthropic_client(api_key, timeout=30.0)
        kwargs: Dict[str, Any] = {"model": model_id, "messages": messages}
        if system_prompt:
            kwargs["system"] = system_prompt
        if tools:
            kwargs["tools"] = tools
        resp = await client.messages.count_tokens(**kwargs)
        return int(getattr(resp, "input_tokens", 0) or 0)
    except Exception as exc:  # noqa: BLE001
        logger.debug("count_tokens for %s failed (%s) — falling back", model_id, exc)
        return None


# ---------------------------------------------------------------------------
# Estimator
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
    """Estimate a call's USD band on ``model_id`` as served by ``provider_type``.

    Rates and ``pricing_source`` come from the model registry for whatever
    provider is given — ``zero`` for Ollama, ``unknown`` (and $0) when the
    registry cannot price the pair. Only the token counter is per provider.
    """
    source, (in_rate, out_rate, _, _) = get_registry().get_call_pricing(
        model_id, provider_type
    )

    text = _flatten_message_text(messages)
    if system_prompt:
        text = system_prompt + "\n" + text

    input_tokens: Optional[int] = None
    method = "char_heuristic"
    if provider_type == "anthropic":
        input_tokens = await _count_tokens_anthropic(
            model_id, messages, system_prompt, tools
        )
        if input_tokens is not None:
            method = "anthropic_count_tokens"
    elif provider_type == "openai":
        input_tokens, method = _count_tokens_openai(model_id, text)
    if input_tokens is None:
        input_tokens = _char_heuristic_tokens(text)

    low_usd = input_tokens * in_rate
    return CostEstimate(
        provider_type=provider_type,
        model_id=model_id,
        input_tokens=input_tokens,
        output_tokens_max=max_tokens,
        low_usd=low_usd,
        high_usd=low_usd + max_tokens * out_rate,
        pricing_source=source,
        token_count_method=method,
    )
