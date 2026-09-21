# What a model costs per token, for the agent layer. The catalog lives here and
# only here -- a second rate table in TypeScript would be one repricing away from
# disagreeing with the dashboard about what a run cost.
#
# Rates rather than a priced call: they are static per model, so the agent asks
# once and multiplies its own token counts. Pricing every call over HTTP would put
# a round trip in the loop's hot path and a failure mode in the one place that must
# never lose a spend event.

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header

from core.agents.internal_auth import authorise
from core.routing import Auth, RouterMeta

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/internal/pricing",
    tags=["internal-pricing"],
    auth=Auth.ROUTER_MANAGED,
    reason=(
        "A shared secret: the caller is the agent layer, not a session. Reachability\n"
        "is the NetworkPolicy's job since ADR 0014, not a loopback check."
    ),
)
logger = logging.getLogger(__name__)


@router.get("/rates")
async def rates(
    model_id: str,
    provider_type: str,
    authorization: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Per-token USD rates for one model, plus how confidently they resolved."""
    authorise(authorization, "pricing lookup")

    from core.llm.providers.registry import get_registry

    provider_type, model_id = priced_as(provider_type, model_id)
    registry = get_registry()
    input_per_token, output_per_token = registry.get_cost_rates(model_id, provider_type)
    cache_read, cache_write = registry.get_cache_rates(model_id, provider_type)

    # Carried through rather than resolved away: a $0 call priced from a real
    # catalog entry and a $0 call nobody could price look identical on a ledger,
    # and the fix for each is nothing alike. The agent journals this beside the
    # figure so a run's cost can say how much to trust itself.
    return {
        "input": input_per_token,
        "output": output_per_token,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "source": registry.get_pricing_source(model_id, provider_type),
    }


# The agent layer calls one gateway and says so, but a gateway bills nothing of
# its own: the catalog is keyed by whoever actually served the model. Resolved
# here because this is where the catalog lives, and asking the agent to know
# would be the second copy of it this module exists to prevent -- which is also
# why this is public: anything that needs the rate needs this first.
#
# Who serves a model is configuration, never a guess from its name: "llama" on a
# commercial host is a paid call, and a self-hosted endpoint may well answer to
# "gpt-4o". When neither the id nor the caller names a provider, the configured
# default record is the answer; when no record resolves, "unknown", which the
# agent already treats as unpriced rather than free.
def priced_as(provider_type: str, model_id: str) -> tuple[str, str]:
    from core.llm.providers.registry import _PRICED_PROVIDERS

    named, _, bare = model_id.partition("/")
    if bare and named.lower() in _PRICED_PROVIDERS:
        return named.lower(), bare
    if provider_type in _PRICED_PROVIDERS:
        return provider_type, model_id
    return _default_provider_type(), model_id


def _default_provider_type() -> str:
    from core.llm.router.router import get_default_provider_spec

    try:
        provider = get_default_provider_spec()
    except Exception as exc:  # noqa: BLE001
        logger.debug("default provider lookup failed: %s", exc)
        return "unknown"
    return provider.provider_type if provider is not None else "unknown"
