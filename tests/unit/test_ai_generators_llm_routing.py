"""#413 PR4b — the AI draft generators route text through LLMRouter.

These lock the migration contract: WorkflowAIGenerator / AgentAIGenerator
call ``LLMRouter().chat`` (not ``ClaudeService`` directly) with the right
provider-agnostic ``service_config``, and translate a no-provider
``ValueError`` into a clean error dict rather than raising.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from services.agent_ai_generator import AgentAIGenerator  # noqa: E402
from services.workflow_ai_generator import WorkflowAIGenerator  # noqa: E402

pytestmark = pytest.mark.unit

_NO_TOOLS = {"use_backend_tools": False, "use_mcp_tools": False}


def _patch_router():
    """Patch the lazily-imported LLMRouter; return (patch_ctx, chat_mock)."""
    chat = AsyncMock()
    p = patch("services.llm_router.LLMRouter")
    return p, chat


@pytest.mark.asyncio
async def test_workflow_generator_routes_through_llmrouter():
    gen = WorkflowAIGenerator()
    p, chat = _patch_router()
    chat.return_value = '{"foo": 1}'
    with p as RouterCls:
        RouterCls.return_value.chat = chat
        await gen.generate("build a phishing triage workflow")

    chat.assert_awaited_once()
    _, kwargs = chat.call_args
    assert kwargs["service_config"] == _NO_TOOLS
    assert kwargs["enable_thinking"] is False
    assert kwargs["max_tokens"] == 4096
    assert kwargs["system_prompt"]  # non-empty


@pytest.mark.asyncio
async def test_workflow_generator_no_provider_returns_clean_error():
    gen = WorkflowAIGenerator()
    p, chat = _patch_router()
    chat.side_effect = ValueError("API key not configured.")
    with p as RouterCls:
        RouterCls.return_value.chat = chat
        result = await gen.generate("x")

    assert result["success"] is False
    assert result["draft"] is None
    assert "API key not configured" in result["error"]


@pytest.mark.asyncio
async def test_workflow_generator_empty_response():
    gen = WorkflowAIGenerator()
    p, chat = _patch_router()
    chat.return_value = ""
    with p as RouterCls:
        RouterCls.return_value.chat = chat
        result = await gen.generate("x")

    assert result["success"] is False
    assert "Empty response" in result["error"]


@pytest.mark.asyncio
async def test_agent_generator_routes_through_llmrouter():
    gen = AgentAIGenerator()
    p, chat = _patch_router()
    chat.return_value = '{"foo": 1}'
    with p as RouterCls:
        RouterCls.return_value.chat = chat
        await gen.generate("a phishing specialist agent")

    chat.assert_awaited_once()
    _, kwargs = chat.call_args
    assert kwargs["service_config"] == _NO_TOOLS
    assert kwargs["enable_thinking"] is False


@pytest.mark.asyncio
async def test_agent_generator_no_provider_returns_clean_error():
    gen = AgentAIGenerator()
    p, chat = _patch_router()
    chat.side_effect = ValueError("API key not configured.")
    with p as RouterCls:
        RouterCls.return_value.chat = chat
        result = await gen.generate("x")

    assert result["success"] is False
    assert result["draft"] is None
    assert "API key not configured" in result["error"]


@pytest.mark.asyncio
async def test_agent_generator_empty_response():
    gen = AgentAIGenerator()
    p, chat = _patch_router()
    chat.return_value = None
    with p as RouterCls:
        RouterCls.return_value.chat = chat
        result = await gen.generate("x")

    assert result["success"] is False
    assert "Empty response" in result["error"]
