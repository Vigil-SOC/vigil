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
) -> Optional[float]:
    """Compute USD cost of a single LLM call, or ``None`` if it cannot be priced.

    Looks up per-token and per-provider cache rates from the model registry
    in one catalog read. Cache tokens are billed at provider-specific rates
    (#184 Phase 3): Anthropic ephemeral cache reads at 0.1× input, writes at
    1.25× input; OpenAI cached input at 0.5×.

    Unpriced is stored as absence, never as zero (#984 decision 7): a missing
    model/provider, a failed registry lookup, or a model whose pricing source
    is ``unknown`` returns ``None``. ``exact`` / ``heuristic`` / ``zero``
    return a number, which may be ``0.0`` for a genuinely unbilled model.
    """
    if not model_id or not provider_type:
        logger.warning(
            "compute_call_cost: missing model_id/provider_type (got %r / %r); "
            "recording the call as unpriced",
            model_id,
            provider_type,
        )
        return None
    try:
        from core.llm.providers.registry import get_registry

        source, rates = get_registry().get_call_pricing(model_id, provider_type)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "compute_call_cost: model_registry lookup failed for %s/%s (%s); "
            "recording the call as unpriced",
            provider_type,
            model_id,
            exc,
        )
        return None
    if source == "unknown":
        return None
    in_rate, out_rate, cache_read_rate, cache_creation_rate = rates
    return (
        input_tokens * in_rate
        + output_tokens * out_rate
        + cache_read_tokens * cache_read_rate
        + cache_creation_tokens * cache_creation_rate
    )
