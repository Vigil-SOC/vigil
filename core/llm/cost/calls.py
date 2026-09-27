import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CallQuote:
    """One catalog read: the dollar, the four per-token rates behind it, and
    when those rates were fetched. Unpriced is all nulls — the ``0.0`` defaults
    ``get_rates`` returns for ``pricing_source`` ``unknown`` are not a price.
    """

    cost_usd: Optional[float]
    input_cost_per_token: Optional[float]
    output_cost_per_token: Optional[float]
    cache_read_cost_per_token: Optional[float]
    cache_write_cost_per_token: Optional[float]
    rates_fetched_at: Optional[str]

    @staticmethod
    def unpriced() -> "CallQuote":
        return CallQuote(None, None, None, None, None, None)


def quote_call(
    model_id: Optional[str],
    provider_type: Optional[str],
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> CallQuote:
    """Price one call from a single ``get_rates`` read.

    Unpriced is absence, never zero (#984 decision 7): a missing
    model/provider, a failed registry lookup, or ``pricing_source``
    ``unknown`` returns :meth:`CallQuote.unpriced`. ``exact`` / ``zero``
    return a number, which may be ``0.0`` for a genuinely unbilled model,
    along with the rates and fetch time that produced it.
    """
    if not model_id or not provider_type:
        logger.warning(
            "compute_call_cost: missing model_id/provider_type (got %r / %r); "
            "recording the call as unpriced",
            model_id,
            provider_type,
        )
        return CallQuote.unpriced()
    try:
        # Late import: the registry imports the router, which imports this module.
        from core.llm.providers.registry import get_registry

        resolved = get_registry().get_rates(model_id, provider_type)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "compute_call_cost: model_registry lookup failed for %s/%s (%s); "
            "recording the call as unpriced",
            provider_type,
            model_id,
            exc,
        )
        return CallQuote.unpriced()
    if resolved.get("pricing_source") == "unknown":
        return CallQuote.unpriced()
    in_rate = resolved["input"]
    out_rate = resolved["output"]
    cache_read_rate = resolved["cache_read"]
    cache_write_rate = resolved["cache_write"]
    return CallQuote(
        input_tokens * in_rate
        + output_tokens * out_rate
        + cache_read_tokens * cache_read_rate
        + cache_creation_tokens * cache_write_rate,
        in_rate,
        out_rate,
        cache_read_rate,
        cache_write_rate,
        resolved.get("rates_fetched_at"),
    )


def compute_call_cost(
    model_id: Optional[str],
    provider_type: Optional[str],
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> Optional[float]:
    """USD cost of a single LLM call, or ``None`` if it cannot be priced.

    The router metrics path wants only the dollar. The rates and fetch time
    from the same read are on :func:`quote_call`.
    """
    return quote_call(
        model_id,
        provider_type,
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_creation_tokens,
    ).cost_usd
