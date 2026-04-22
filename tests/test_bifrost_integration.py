"""Bifrost sidecar smoke test (GH #88).

Skipped by default. Runs only when BIFROST_URL is set and points at a
live Bifrost instance. Use this as the end-to-end gate in the
verification steps in docs/bifrost/README.md.

Invoke with::

    BIFROST_URL=http://localhost:8080 pytest -m integration \\
        tests/test_bifrost_integration.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("BIFROST_URL"),
        reason="BIFROST_URL not set — Bifrost sidecar not running",
    ),
]


@pytest.mark.asyncio
async def test_bifrost_health_endpoint():
    """Bifrost /health should return 200 when the sidecar is up."""
    import httpx

    url = os.environ["BIFROST_URL"].rstrip("/")
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{url}/health")
    assert resp.status_code == 200, (
        f"Bifrost health check failed: {resp.status_code} {resp.text}"
    )


@pytest.mark.asyncio
async def test_bifrost_chat_completion_via_ollama():
    """A chat completion routed to Ollama through Bifrost should return content.

    Requires a running Ollama with the model named in OLLAMA_DEFAULT_MODEL
    (defaults to llama3.1:8b for the smoke test — smaller than the
    env.example production default).
    """
    from services.llm_router import LLMRouter, ProviderSpec

    model = os.getenv("OLLAMA_SMOKE_MODEL", "llama3.1:8b")
    spec = ProviderSpec(
        provider_id="ollama-smoke",
        provider_type="ollama",
        base_url=os.getenv("OLLAMA_URL", "http://localhost:11434"),
        api_key_ref=None,
        default_model=model,
        config={},
    )
    router = LLMRouter(bifrost_url=os.environ["BIFROST_URL"])
    result = await router.dispatch(
        provider=spec,
        messages=[{"role": "user", "content": "Say the single word 'ping'."}],
        max_tokens=16,
    )
    assert result["path"] == "bifrost"
    assert result["provider"] == "ollama"
    assert isinstance(result.get("content"), str) and result["content"].strip(), (
        "Bifrost→Ollama returned empty content"
    )
