"""Single source of truth for Anthropic SDK client construction.

Per GH #84 PR-B, all LLM traffic in Vigil routes through the Bifrost
gateway — including Anthropic traffic that used to bypass it. The SDK
client is pointed at Bifrost's Anthropic-compatible passthrough so
extended thinking, native prompt caching, and tool-use round-trip
unchanged while Bifrost layers in exact-hash caching, centralized cost
tracking, and budget enforcement.

Call sites that need an Anthropic client should import
``create_anthropic_client`` / ``create_async_anthropic_client`` from
this module instead of instantiating ``Anthropic()`` directly. This
keeps the gateway-routing decision in one place and makes it trivial to
audit (grep for ``Anthropic(``).

Key-validation endpoints that deliberately hit the upstream provider to
verify a user-supplied credential (e.g. ``backend/api/llm_providers.py``)
are the only exception and must still call ``Anthropic()`` directly.
"""

from __future__ import annotations

import os
from typing import Optional

_DEFAULT_TIMEOUT = 1800.0


def _bifrost_anthropic_base_url() -> str:
    """Return the endpoint Anthropic traffic should hit.

    Normally this is Bifrost's Anthropic-compatible passthrough at
    ``/anthropic`` (alongside its OpenAI-format surface), which preserves
    extended thinking, ``cache_control`` blocks, and the cache-token usage
    counters while Bifrost layers in caching/cost tracking.

    In deployments where Bifrost cannot reach the Anthropic upstream — e.g.
    ``api.anthropic.com`` is firewalled and an internal Anthropic-compatible
    proxy (LiteLLM, etc.) must be used instead — set ``ANTHROPIC_BASE_URL``
    to that proxy's root. Bifrost's built-in ``anthropic`` provider ignores
    its own ``network_config.base_url``, so the redirect has to happen here
    on the SDK client. When the override is set we point the SDK straight at
    it (the SDK appends ``/v1/messages`` and ``/v1/messages/count_tokens``),
    bypassing Bifrost for Anthropic traffic.
    """
    override = os.getenv("ANTHROPIC_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")
    base = os.getenv("BIFROST_URL", "http://bifrost:8080").rstrip("/")
    return f"{base}/anthropic"


def create_anthropic_client(api_key: str, *, timeout: float = _DEFAULT_TIMEOUT):
    """Synchronous Anthropic client routed through Bifrost."""
    from anthropic import Anthropic  # lazy so tests without the SDK still import

    return Anthropic(
        api_key=api_key,
        base_url=_bifrost_anthropic_base_url(),
        timeout=timeout,
    )


def create_async_anthropic_client(
    api_key: str, *, timeout: float = _DEFAULT_TIMEOUT
):
    """Async Anthropic client routed through Bifrost."""
    from anthropic import AsyncAnthropic  # lazy

    return AsyncAnthropic(
        api_key=api_key,
        base_url=_bifrost_anthropic_base_url(),
        timeout=timeout,
    )
