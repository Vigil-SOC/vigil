"""Chat grants read_skill, so the turn must carry the skills index (#1883)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.agents import manager as agent_manager
from core.agents import prompts
from services.api.routers import claude

pytestmark = pytest.mark.unit


def _skill(root, name, description):
    folder = root / name
    folder.mkdir()
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nbody\n"
    )


@pytest.fixture
def turn(monkeypatch, tmp_path):
    """Run one chat turn; returns the payload the relay saw."""
    monkeypatch.setattr(prompts, "skill_roots", lambda *_: [tmp_path])
    monkeypatch.setattr(claude, "live_mcp_tools", lambda _registry: [])
    monkeypatch.setattr(
        claude, "provider_for", lambda _p: SimpleNamespace(provider_type="gemini")
    )
    monkeypatch.setattr(claude, "model_for", lambda _p, model: model)
    monkeypatch.setattr(
        claude, "_resolve_provider_model_for_request", lambda *_: ("gemini", "m")
    )

    async def run(agent=None):
        sent: dict = {}

        async def _relay(payload, *_args):
            sent.update(payload)
            yield ""

        monkeypatch.setattr(claude, "_relay", _relay)
        agents = {"a1": agent} if agent else {}
        monkeypatch.setattr(
            agent_manager,
            "AgentManager",
            lambda: SimpleNamespace(agents=agents),
        )
        response = await claude.chat_stream(
            claude.ChatRequest(
                messages=[{"role": "user", "content": "hi"}],
                agent_id="a1" if agent else None,
            ),
            current_user=SimpleNamespace(username="nestor", user_id="u-1"),
            registry=MagicMock(),
            workflows=MagicMock(),
        )
        async for _ in response.body_iterator:
            pass
        return sent

    return run


@pytest.mark.asyncio
async def test_default_chat_lists_skills_and_new_ones_show_next_turn(turn, tmp_path):
    _skill(tmp_path, "banana-check", "Use when asked to run the banana check.")
    first = await turn()
    assert "<available_skills>" in first["system_prompt"]
    assert "- banana-check: Use when asked to run the banana check." in (
        first["system_prompt"]
    )
    assert "read_skill" in first["config"]

    _skill(tmp_path, "cherry-check", "Use for cherries.")
    second = await turn()
    assert "- cherry-check: Use for cherries." in second["system_prompt"]


@pytest.mark.asyncio
async def test_agent_without_read_skill_gets_no_index(turn, tmp_path):
    _skill(tmp_path, "banana-check", "Use when asked to run the banana check.")
    agent = SimpleNamespace(
        system_prompt="You are A.", recommended_tools=["get_finding"]
    )
    sent = await turn(agent)
    assert "<available_skills>" not in sent["system_prompt"]
    assert "read_skill" not in sent["config"]


@pytest.mark.asyncio
async def test_agent_prompt_already_carrying_the_index_is_not_doubled(turn, tmp_path):
    _skill(tmp_path, "banana-check", "Use when asked to run the banana check.")
    agent = SimpleNamespace(
        system_prompt=prompts._skills_section(["read_skill"], None),
        recommended_tools=["read_skill"],
    )
    sent = await turn(agent)
    assert sent["system_prompt"].count("<available_skills>") == 1
