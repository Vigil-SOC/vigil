"""Which provider and model a run should be dispatched to.

Bifrost routes ``<provider>/<model>`` and matches a bare name to whichever
provider claims it first, so the pair has to travel together.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Vertex and Bedrock resell Claude, so this is an allowlist, not a test for
# Anthropic.
_SERVES_CLAUDE = frozenset({"anthropic", "vertex", "bedrock"})


def provider_for(provider_id: Optional[str]):
    """Pick the provider a request should route through.

    The model picker can send the model as ``provider_id::model_id`` (#348); a
    bare id has no provider and falls back to the configured default rather
    than the Anthropic SDK, which 503s on an Ollama-only deployment.
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
    """Model id to send, pinned to the provider's default if it can't serve it."""
    model = requested_model or provider.default_model
    if can_serve(provider, model):
        return model

    if model == provider.default_model:
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

    Without a catalogue everything but a ``claude-*`` id goes through:
    Bifrost's own error beats a silent substitution.
    """
    catalogue = _catalogue(provider)
    if catalogue is not None:
        return model in catalogue
    return not model.startswith("claude-") or provider.provider_type in _SERVES_CLAUDE


def _catalogue(provider) -> Optional[set]:
    """Model ids this provider is known to serve, or None when that isn't known."""
    try:
        from core.llm.providers.registry import catalogue_of

        cached = catalogue_of(provider.provider_id)
        return set(cached) if cached else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("catalogue lookup failed for %s: %s", provider.provider_id, exc)
        return None


def resolve_component(component: str) -> Optional[Tuple[str, str]]:
    """The ``(provider_type, model)`` a component's assignment resolves to.

    ``provider_type`` because the row id means nothing to Bifrost. None leaves
    the caller its own default.
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
            "%s resolves to provider %s, which has no row", component, provider_id
        )
        return None
    return provider.provider_type, model_for(provider, model_id)
