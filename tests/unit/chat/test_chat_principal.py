"""A chat turn hands the agent layer the signed-in person, signed by the API (#1087)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.auth import tool_principal
from services.api.routers import claude

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_the_turn_carries_a_principal_for_the_signed_in_user(monkeypatch):
    sent = {}

    async def _relay(payload, *_args):
        sent.update(payload)
        yield ""

    monkeypatch.setattr(
        claude, "_resolve_provider_model_for_request", lambda *_: (None, "m")
    )
    monkeypatch.setattr(
        claude, "provider_for", lambda _: SimpleNamespace(provider_type="gemini")
    )
    monkeypatch.setattr(claude, "model_for", lambda _p, model: model)
    monkeypatch.setattr(claude, "live_mcp_tools", lambda _: [])
    monkeypatch.setattr(claude, "_relay", _relay)

    response = await claude.chat_stream(
        claude.ChatRequest(messages=[{"role": "user", "content": "close case-1"}]),
        current_user=SimpleNamespace(username="nestor", user_id="u-1"),
        registry=MagicMock(),
    )
    async for _ in response.body_iterator:
        pass

    assert tool_principal.verify(sent["principal"]) == "nestor"
