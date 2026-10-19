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
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header

from core.agents.internal_auth import authorise
from core.llm.cost.budget import get_active_vk, should_enforce
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
    if resolved["pricing_source"] == "unknown" and await _reread_rates(provider_type):
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


# A provider added after this process last read the gateway's datasheet has no
# rates here until the next catalogue refresh, up to five minutes later, and a hunt
# started in that window parks on its first iteration as unpriced (#2290). The API
# runs more than one worker and only the one that took the write syncs at once, so
# the read happens where the question lands: one sheet, at most once per type per
# interval, since an id the gateway truly cannot price asks on every call.
_REREAD_INTERVAL_S = 30.0
_last_reread: Dict[str, float] = {}


async def _reread_rates(provider_type: str) -> bool:
    """Read ``provider_type``'s datasheet into this process. True when it answered."""
    from core.llm.providers.registry import VALID_PROVIDER_TYPES

    if provider_type not in VALID_PROVIDER_TYPES:
        return False
    now = time.monotonic()
    last = _last_reread.get(provider_type)
    if last is not None and now - last < _REREAD_INTERVAL_S:
        return False
    _last_reread[provider_type] = now

    from core.llm.bifrost.admin import record_gateway_rates

    try:
        sheets = await record_gateway_rates([provider_type])
    except Exception as exc:  # noqa: BLE001 - unpriced stays the answer
        logger.debug("rate re-read for %s failed: %s", provider_type, exc)
        return False
    return any(sheets.values())


# The virtual key the agent sends as x-bf-vk, or null when the budget is not
# enforced. should_enforce() decides, so DEV_MODE and LLM_BUDGET_UNLIMITED mean the
# same thing to agent traffic as to the router's. Asked for rather than put in the
# RunSpec: a checkpointed spec would resume carrying a key rotated since.
@router.get("/vk")
async def virtual_key(
    authorization: Optional[str] = Header(default=None),
) -> Dict[str, Optional[str]]:
    authorise(authorization, "virtual key lookup")
    return {"vk": get_active_vk() if should_enforce() else None}


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

    # OpenRouter ids are "<vendor>/<model>" under OpenRouter itself, so the vendor
    # prefix is not a provider to price as; the registry falls back to the vendor's
    # sheet when OpenRouter's lacks the id.
    if provider_type.lower() == "openrouter":
        return "openrouter", model_id
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
