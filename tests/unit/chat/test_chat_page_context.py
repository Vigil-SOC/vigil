"""A turn with no model names the page and uses chat_default (#1328)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.api.routers import claude

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_turn_without_a_model_names_the_page_and_uses_chat_default(monkeypatch):
    sent: dict = {}
    seen: dict = {}

    class _Registry:
        def resolve_model_for_component(self, category, agent_override=None):
            seen["category"] = category
            seen["override"] = agent_override
            return ("gemini", "gemini-flash-latest")

    async def _relay(payload, request, *_args):
        sent["system_prompt"] = payload["system_prompt"]
        sent["config"] = payload["config"]
        sent["model"] = request.model
        yield ""

    monkeypatch.setattr(claude, "get_registry", lambda: _Registry())
    monkeypatch.setattr(
        claude,
        "provider_for",
        lambda _provider: SimpleNamespace(provider_type="gemini"),
    )
    monkeypatch.setattr(claude, "model_for", lambda _provider, model: model)
    monkeypatch.setattr(claude, "live_mcp_tools", lambda _registry: [])
    monkeypatch.setattr(claude, "_relay", _relay)

    response = await claude.chat_stream(
        claude.ChatRequest(
            messages=[{"role": "user", "content": "what is on this page?"}],
            page_context="overview",
            case_id="CASE-9",
        ),
        current_user=SimpleNamespace(username="nestor", user_id="u-1"),
        registry=MagicMock(),
    )
    async for _ in response.body_iterator:
        pass

    assert seen["category"] == "chat_default"
    assert seen["override"] is None
    assert sent["model"] == "gemini-flash-latest"
    assert "model: gemini-flash-latest" in sent["config"]
    assert (
        sent["system_prompt"]
        == "The analyst opened this from page overview about case CASE-9."
    )
