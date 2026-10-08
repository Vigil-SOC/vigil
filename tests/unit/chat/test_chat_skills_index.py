"""Chat grants read_skill, so its prompt carries the skills index (#1883)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from core.agents.prompts import _skills_section
from core.skills.skill_library import load_skills
from services.api.routers import claude

pytestmark = pytest.mark.unit


def _write_skill(root: Path, name: str, description: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def _install(monkeypatch, root: Path, agents: dict | None = None) -> dict:
    """Stub the provider/relay/agent seams and point skills at ``root``."""
    sent: dict = {}

    async def _relay(payload, *_args):
        sent["system_prompt"] = payload["system_prompt"]
        sent["config"] = payload["config"]
        yield ""

    monkeypatch.setattr(
        claude, "_resolve_provider_model_for_request", lambda *_: (None, "m")
    )
    monkeypatch.setattr(
        claude,
        "provider_for",
        lambda _provider: SimpleNamespace(provider_type="gemini"),
    )
    monkeypatch.setattr(claude, "model_for", lambda _provider, model: model)
    monkeypatch.setattr(claude, "live_mcp_tools", lambda _registry: [])
    monkeypatch.setattr(claude, "disabled_agent_ids", lambda: set())
    monkeypatch.setattr(claude, "_relay", _relay)
    monkeypatch.setattr(
        "core.agents.prompts.skill_roots", lambda *_args, **_kwargs: [root]
    )

    if agents is not None:

        class _Mgr:
            def __init__(self):
                self.agents = agents

        monkeypatch.setattr("core.agents.manager.AgentManager", _Mgr)

    return sent


async def _turn(sent: dict, agent_id: str | None = None) -> dict:
    sent.clear()
    response = await claude.chat_stream(
        claude.ChatRequest(
            messages=[{"role": "user", "content": "run the banana check"}],
            agent_id=agent_id,
        ),
        current_user=SimpleNamespace(username="nestor", user_id="u-1"),
        registry=MagicMock(),
        workflows=MagicMock(),
    )
    async for _ in response.body_iterator:
        pass
    return dict(sent)


def _declared_ids(sent: dict) -> list:
    return [tool["id"] for tool in yaml.safe_load(sent["config"])["tools"]]


@pytest.mark.asyncio
async def test_no_agent_id_gets_the_skills_index(monkeypatch, tmp_path):
    _write_skill(tmp_path, "banana-check", "Run the banana check.")
    sent = _install(monkeypatch, tmp_path)

    await _turn(sent)

    assert "<available_skills>" in sent["system_prompt"]
    assert "- banana-check: Run the banana check." in sent["system_prompt"]
    assert "read_skill" in _declared_ids(sent)


@pytest.mark.asyncio
async def test_agent_without_read_skill_gets_no_block(monkeypatch, tmp_path):
    _write_skill(tmp_path, "banana-check", "Run the banana check.")
    agent = SimpleNamespace(
        system_prompt="You are a narrow agent.",
        recommended_tools=["get_finding"],
    )
    sent = _install(monkeypatch, tmp_path, agents={"narrow": agent})

    await _turn(sent, agent_id="narrow")

    assert "<available_skills>" not in sent["system_prompt"]
    assert sent["system_prompt"] == "You are a narrow agent."
    assert "read_skill" not in _declared_ids(sent)


@pytest.mark.asyncio
async def test_agent_prompt_with_the_block_keeps_exactly_one(monkeypatch, tmp_path):
    _write_skill(tmp_path, "banana-check", "Run the banana check.")
    existing = "You are a granted agent.\n\n" + _skills_section(
        ["read_skill"], load_skills([tmp_path])
    )
    agent = SimpleNamespace(
        system_prompt=existing,
        recommended_tools=["get_finding", "read_skill"],
    )
    sent = _install(monkeypatch, tmp_path, agents={"granted": agent})

    await _turn(sent, agent_id="granted")

    assert sent["system_prompt"].count("<available_skills>") == 1
    # _with_page_case strips the prompt, so the trailing newline goes.
    assert sent["system_prompt"] == existing.strip()
    assert "read_skill" in _declared_ids(sent)


@pytest.mark.asyncio
async def test_a_skill_saved_between_turns_is_offered_on_the_next(
    monkeypatch, tmp_path
):
    _write_skill(tmp_path, "banana-check", "Run the banana check.")
    sent = _install(monkeypatch, tmp_path)

    await _turn(sent)
    assert "banana-check" in sent["system_prompt"]
    assert "cherry-check" not in sent["system_prompt"]

    _write_skill(tmp_path, "cherry-check", "Run the cherry check.")
    await _turn(sent)
    assert "- cherry-check: Run the cherry check." in sent["system_prompt"]
