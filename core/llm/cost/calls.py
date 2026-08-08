import logging
from typing import Optional

logger = logging.getLogger(__name__)


def compute_call_cost(
    model_id: Optional[str],
    provider_type: Optional[str],
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Compute USD cost of a single LLM call.

    Looks up per-token rates from ``core.llm.providers.registry.get_cost_rates()``
    and per-provider cache multipliers from ``get_cache_rates()``. Cache
    tokens are billed at provider-specific rates (#184 Phase 3): Anthropic
    ephemeral cache reads at 0.1× input, writes at 1.25× input; OpenAI
    cached input at 0.5×. Counting them at full input rate (the pre-#184
    behavior) over-bills cache reads by 10× and under-bills cache writes
    by 25%, so this matters for any workload that uses prompt caching —
    which after #84 PR-C is most of Vigil's traffic.

    GH #84 PR-E removed the previous Sonnet-pricing fallback: with
    per-component model selection (#89) active, silently billing a GPT-4o
    or Ollama call at Sonnet rates would misattribute cost. On an
    unresolved model/provider we return 0.0 and log at WARNING so the
    call surfaces as a visible zero on the ``/analytics/cost`` dashboard
    rather than hiding inside a misattributed bucket.
    """
    if not model_id or not provider_type:
        logger.warning(
            "compute_call_cost: missing model_id/provider_type (got %r / %r); "
            "recording cost as $0.00 (GH #84 PR-E)",
            model_id,
            provider_type,
        )
        return 0.0
    try:
        from core.llm.providers.registry import get_registry

        registry = get_registry()
        in_rate, out_rate = registry.get_cost_rates(model_id, provider_type)
        cache_read_rate, cache_creation_rate = registry.get_cache_rates(
            model_id, provider_type
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "compute_call_cost: model_registry lookup failed for %s/%s (%s); "
            "recording cost as $0.00",
            provider_type,
            model_id,
            exc,
        )
        return 0.0
    return (
        input_tokens * in_rate
        + output_tokens * out_rate
        + cache_read_tokens * cache_read_rate
        + cache_creation_tokens * cache_creation_rate
    )
