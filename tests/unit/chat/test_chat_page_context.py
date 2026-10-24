"""A turn with no model names the page and uses chat_default (#1328); what
changes between turns rides on the latest question, not the system prompt (#2863)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.agents import manager as agent_manager
from core.cases.case_brief import RULES
from services.api.routers import claude

pytestmark = pytest.mark.unit

MCP = [{"name": "vt_lookup", "description": "look up a hash"}]


@pytest.fixture
def turn(monkeypatch):
    """Run one chat turn; returns what the relay was handed."""
    seen: dict = {"agent_managers": 0}

    class _Registry:
        def resolve_model_for_component(self, category, agent_override=None):
            seen["category"] = category
            seen["override"] = agent_override
            return ("gemini", "gemini-flash-latest")

    def _manager():
        seen["agent_managers"] += 1
        return SimpleNamespace(
            agents={
                "a1": SimpleNamespace(
                    system_prompt="You are A.", recommended_tools=None
                )
            }
        )

    monkeypatch.setattr(claude, "get_registry", lambda: _Registry())
    monkeypatch.setattr(
        claude,
        "provider_for",
        lambda _provider: SimpleNamespace(provider_type="gemini"),
    )
    monkeypatch.setattr(claude, "model_for", lambda _provider, model: model)
    monkeypatch.setattr(claude, "live_mcp_tools", lambda _registry: MCP)
    monkeypatch.setattr(claude, "disabled_agent_ids", lambda: set())
    monkeypatch.setattr(agent_manager, "AgentManager", _manager)

    async def run(brief="", messages=None, **request):
        sent: dict = {}

        async def _relay(payload, _request, model, *args):
            sent.update(payload, model=model, question=args[-2])
            yield ""

        async def case_brief(case_id, _workflows):
            return brief

        monkeypatch.setattr(claude, "_relay", _relay)
        monkeypatch.setattr(claude, "case_brief", case_brief)
        registry = MagicMock()
        registry.tool_servers.return_value = {"vt_lookup": "virustotal"}
        response = await claude.chat_stream(
            claude.ChatRequest(
                messages=messages or [{"role": "user", "content": "what is here?"}],
                **request,
            ),
            current_user=SimpleNamespace(username="nestor", user_id="u-1"),
            registry=registry,
            workflows=MagicMock(),
        )
        async for _ in response.body_iterator:
            pass
        return sent

    run.seen = seen
    return run


@pytest.mark.asyncio
async def test_turn_without_a_model_names_the_page_and_uses_chat_default(turn):
    sent = await turn(
        "<case_data>\nev-1\n</case_data>", page_context="overview", case_id="CASE-9"
    )
    assert turn.seen["category"] == "chat_default"
    assert turn.seen["override"] is None
    assert sent["model"] == "gemini-flash-latest"
    assert "model: gemini-flash-latest" in sent["config"]
    assert sent["system_prompt"].startswith(f"{claude.LATEST_ONLY}\n\n{RULES}")
    assert "find_integration_tools" in sent["system_prompt"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert sent["turns"][-1]["content"] == (
        "The analyst opened this from page overview about case CASE-9.\n"
        f"Today is {today} (UTC).\n"
        "Connected integrations: virustotal.\n"
        "<case_data>\nev-1\n</case_data>\n\n"
        "what is here?"
    )
    # History stores the question, not the context block.
    assert sent["question"] == "what is here?"


@pytest.mark.asyncio
async def test_system_prompt_is_byte_identical_across_turns(turn):
    first = await turn(
        "<case_data>\nrun r-1: running, ev-1\n</case_data>",
        agent_id="a1",
        page_context="overview",
        case_id="CASE-9",
    )
    assert turn.seen["agent_managers"] == 1
    second = await turn(
        "<case_data>\nrun r-1: completed, ev-1, ev-2\n</case_data>",
        messages=[
            {"role": "user", "content": "what is here?"},
            {"role": "assistant", "content": "ev-1."},
            {"role": "user", "content": "and now?"},
        ],
        agent_id="a1",
        page_context="cases/CASE-9",
        case_id="CASE-9",
    )
    assert turn.seen["agent_managers"] == 2
    assert second["system_prompt"] == first["system_prompt"]
    assert first["system_prompt"].startswith("You are A.")
    assert "ev-2" in second["turns"][-1]["content"]
    assert "page cases/CASE-9" in second["turns"][-1]["content"]
    # Earlier turns are the prose the client sent.
    assert second["turns"][0]["content"] == "what is here?"
