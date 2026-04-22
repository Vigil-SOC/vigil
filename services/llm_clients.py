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
    """Return the Bifrost endpoint Anthropic traffic should hit.

    Bifrost exposes an Anthropic-compatible passthrough at ``/anthropic``
    alongside its OpenAI-format surface. Hitting that path with the
    regular Anthropic SDK preserves extended thinking, ``cache_control``
    blocks, and the cache-token usage counters.
    """
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
