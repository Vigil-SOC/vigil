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
    resolved = get_registry().get_rates(model_id, provider_type)

    # Carried through rather than resolved away: a $0 call priced from a real
    # catalog entry and a $0 call nobody could price look identical on a ledger,
    # and the fix for each is nothing alike. The agent journals this beside the
    # figure so a run's cost can say how much to trust itself. ``fetched_at`` is
    # when this process last read the rates off the gateway (null if never).
    return {
        "input": resolved["input"],
        "output": resolved["output"],
        "cache_read": resolved["cache_read"],
        "cache_write": resolved["cache_write"],
        "source": resolved["pricing_source"],
        "fetched_at": resolved["rates_fetched_at"],
    }


# What the agent layer names when it knows only the gateway it called.
GATEWAY = "bifrost"


# The agent layer calls one gateway and says so, but a gateway bills nothing of
# its own: the catalog is keyed by whoever actually served the model. Resolved
# here because this is where the catalog lives, and asking the agent to know
# would be the second copy of it this module exists to prevent -- which is also
# why this is public: anything that needs the rate needs this first.
#
# Who serves a model is configuration, never a guess from its name: "llama" on a
# commercial host is a paid call, and a self-hosted endpoint may well answer to
# "gpt-4o". A provider the caller names is final even when the catalog holds no
# rates for it -- "unknown" is the honest answer, and the agent treats it as
# unpriced rather than free. Only a bare id under the gateway itself falls back
# to the configured default record; when no record resolves, "unknown" again.
def priced_as(provider_type: str, model_id: str) -> tuple[str, str]:
    from core.llm.providers.registry import VALID_PROVIDER_TYPES

    named, _, bare = model_id.partition("/")
    if bare and named.lower() in VALID_PROVIDER_TYPES:
        return named.lower(), bare
    caller = provider_type.lower()
    if caller and caller != GATEWAY:
        return caller, model_id
    return _default_provider_type(), model_id


def _default_provider_type() -> str:
    from core.llm.router.router import get_default_provider_spec

    try:
        provider = get_default_provider_spec()
    except Exception as exc:  # noqa: BLE001
        logger.debug("default provider lookup failed: %s", exc)
        return "unknown"
    return provider.provider_type if provider is not None else "unknown"
