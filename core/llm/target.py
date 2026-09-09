"""Which provider and model a run should be dispatched to.

One home for a question three callers ask: interactive chat, a workflow or hunt
run resolving its config layer, and anything else that has to name a provider
before it can name a model.

The pair travels together on purpose. Bifrost routes ``<provider>/<model>`` and
matches a bare name to whichever provider claims it first, so a model without
the provider it was resolved against is a coin flip on any deployment holding
two that offer the same id. And a model has to be checked against the provider
that will serve it: a stale assignment left pointing at a model the active
provider never had 404s at the gateway.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Provider types that can answer a ``claude-*`` id: Anthropic direct, plus the
# two clouds that resell the models. Used only when the catalogue is unknown; a
# known catalogue answers the question outright. An allowlist rather than a
# test for Anthropic, since treating Vertex and Bedrock as ineligible is what
# discarded a real Claude selection on Vertex and surfaced Gemini's error for
# it.
_SERVES_CLAUDE = frozenset({"anthropic", "vertex", "bedrock"})


def provider_for(provider_id: Optional[str]):
    """Pick the provider a request should route through.

    Precedence:
      1. An explicit ``provider_id`` — the model picker can send the model as
         ``provider_id::model_id`` (#348); look the provider up by id.
      2. The configured default provider (``get_default_provider_spec``) — so a
         *bare* model id (the shape the Chat dock sends) still routes to a
         non-Anthropic default instead of falling through to the Anthropic SDK
         and 503-ing on Ollama-only deployments.

    Returns a ``ProviderSpec`` or ``None``. Lookups are wrapped so a transient
    DB error degrades to the ClaudeService/Anthropic path rather than 500-ing.
    """
    from core.llm.router.router import get_default_provider_spec, get_provider_spec

    provider = None
    if provider_id:
        try:
            provider = get_provider_spec(provider_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("provider lookup failed for %s: %s", provider_id, exc)
            provider = None
    if provider is None:
        try:
            provider = get_default_provider_spec()
        except Exception as exc:  # noqa: BLE001
            logger.debug("default provider lookup failed: %s", exc)
            provider = None
    return provider


def model_for(provider, requested_model: Optional[str]) -> str:
    """Model id to send, pinned to the provider's default if it can't serve it.

    A stale selection — an assignment left pointing at a model the active
    provider never had — would 404 at Bifrost, so it falls back to the
    provider's own default.

    What the provider serves is ``can_serve``'s question. An earlier version
    asked "is this Anthropic?" and pinned every ``claude-*`` id elsewhere,
    which was right for Ollama and OpenAI and wrong for Vertex: Google resells
    Claude, so a Claude id there is a real selection. It was discarded before
    it ever reached the gateway, and the substitute's failure was what surfaced
    — an error about Gemini for a request the operator had pointed at Claude.
    """
    model = requested_model or provider.default_model
    if can_serve(provider, model):
        return model

    if model == provider.default_model:
        # Nothing better to send: the fallback *is* the default. Say so, since
        # the gateway's 404 on its own names a model the operator never chose.
        logger.warning(
            "Provider %s cannot serve its own default_model %s — sending it "
            "anyway; the gateway's error is the only signal left",
            provider.provider_id,
            model,
        )
        return model

    logger.info(
        "Provider %s cannot serve %s — falling back to %s",
        provider.provider_id,
        model,
        provider.default_model,
    )
    return provider.default_model


def can_serve(provider, model: str) -> bool:
    """Whether ``provider`` can be expected to route ``model``.

    A live catalogue answers outright. Without one, the only thing that can be
    said is that a ``claude-*`` id cannot come from a provider that does not
    carry Claude — so everything else is allowed through, and Bifrost's own
    error beats a silent substitution.
    """
    catalogue = _catalogue(provider)
    if catalogue is not None:
        return model in catalogue
    return not model.startswith("claude-") or provider.provider_type in _SERVES_CLAUDE


def _catalogue(provider) -> Optional[set]:
    """Model ids this provider is known to serve, or None when that isn't known.

    Reads the same cache that fills the console's model picker, so a model the
    operator could select is a model this accepts — but only the entries that
    came from a real catalogue. The cache also holds a bootstrap floor of a few
    hardcoded ids for a provider whose models could not be listed, and denying
    from that floor silently swapped a valid selection for the default. A
    mirrored row is exactly that case: it holds no api_key_ref, so an Anthropic
    key configured in Bifrost alone cannot list its own models.
    """
    try:
        from core.llm.providers.registry import catalogue_of

        cached = catalogue_of(provider.provider_id)
        return set(cached) if cached else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("catalogue lookup failed for %s: %s", provider.provider_id, exc)
        return None


def resolve_component(component: str) -> Optional[Tuple[str, str]]:
    """The ``(provider_type, model)`` a component's assignment resolves to.

    ``provider_type`` is the gateway's provider name, which is what rides
    beside the model on the wire — not the row id, which means nothing to
    Bifrost. None when nothing can be resolved, and the caller keeps whatever
    default it had.

    The assignment chain is the registry's (``component`` →  ``chat_default``
    → the default Anthropic provider), and the model it yields is put through
    ``model_for`` like any other: an assignment can be as stale as a chat
    selection, and a workflow run has nobody watching it fail.
    """
    from core.llm.providers.registry import get_registry

    try:
        resolved = get_registry().resolve_for_component(component)
    except Exception as exc:  # noqa: BLE001
        logger.warning("model assignment lookup failed for %s: %s", component, exc)
        return None
    if not resolved:
        return None

    provider_id, model_id = resolved
    provider = provider_for(provider_id)
    if provider is None:
        logger.warning(
            "%s resolves to provider %s, which has no row — leaving the model "
            "unresolved",
            component,
            provider_id,
        )
        return None
    return provider.provider_type, model_for(provider, model_id)
