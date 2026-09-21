"""The read_skill grant and the <available_skills> block (#925)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.agents.builtins import BUILTIN_AGENTS
from core.agents.prompts import _skills_section, render_base_prompt
from core.agents.tool_registry import MANIFEST, execute_backend_tool
from core.llm.chat_layers import _declare
from core.skills.skill_library import load_skills

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent.parent / "skills" / "fixtures"


def test_read_skill_is_a_backend_tool_every_builtin_may_call():
    assert "read_skill" in MANIFEST
    for agent in BUILTIN_AGENTS:
        declared = {t["id"] for t in _declare(agent["recommended_tools"], [])}
        assert "read_skill" in declared, f"{agent['id']} cannot call read_skill"


def test_skills_block_follows_the_grant_not_the_library():
    skills = load_skills([FIXTURES])
    assert _skills_section(["get_finding"], skills) == ""
    assert _skills_section(None, skills) == ""

    granted = _skills_section(["get_finding", "read_skill"], skills)
    assert granted.startswith("<available_skills>")
    assert "- minimal-skill: The smallest skill" in granted
    assert "- full-skill:" in granted
    assert "some-other-name" not in granted

    # A granted agent with nothing on disk is still told the tool exists.
    assert "<available_skills>" in _skills_section(["read_skill"], [])


def test_render_base_prompt_lists_skills_only_for_a_granted_agent():
    skills = load_skills([FIXTURES])
    without = render_base_prompt(role="Reporter", tools=["get_finding"], skills=skills)
    assert "<available_skills>" not in without
    assert "read_skill" not in without

    granted = render_base_prompt(
        role="Reporter", tools=["get_finding", "read_skill"], skills=skills
    )
    assert "<available_skills>" in granted
    assert "minimal-skill" in granted


@pytest.mark.asyncio
async def test_read_skill_dispatches_through_execute_backend_tool(monkeypatch):
    monkeypatch.setattr(
        "core.skills.skill_library.skill_roots", lambda settings=None: [FIXTURES]
    )
    result, handled = await execute_backend_tool("read_skill", {"name": "minimal-skill"})
    assert handled is True
    assert result["content"].startswith("# Minimal skill")

    result, handled = await execute_backend_tool(
        "read_skill", {"name": "full-skill", "file": "../minimal-skill/SKILL.md"}
    )
    assert handled is True
    assert "outside skill" in result["error"]
