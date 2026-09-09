"""Unit tests for recall_entity tool grant and read-only memory prompts (GH #735)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

from core.agents.builtins import BUILTIN_AGENTS, AgentId
from core.agents.prompts import (
    _MEMORY_BLOCK,
    _memory_section,
    render_base_prompt,
)

pytestmark = pytest.mark.unit


def test_recall_entity_in_every_builtin_recommended_tools():
    """Every built-in agent must carry recall_entity in recommended_tools (#735)."""
    assert len(BUILTIN_AGENTS) == 13
    for agent in BUILTIN_AGENTS:
        tools = agent.get("recommended_tools", [])
        assert (
            "recall_entity" in tools
        ), f"Agent {agent['id']} is missing recall_entity in recommended_tools"


def test_memory_block_is_read_only():
    """Memory operations block must mention recall_entity and instruct not to write."""
    assert "recall_entity" in _MEMORY_BLOCK
    assert "read-only" in _MEMORY_BLOCK
    assert "no tool to write" in _MEMORY_BLOCK
    assert "BEFORE starting" not in _MEMORY_BLOCK
    assert "DURING investigation" not in _MEMORY_BLOCK
    assert "AFTER completing" not in _MEMORY_BLOCK


def test_memory_section_gated_on_the_agents_own_grant():
    """The block follows the grant, not the existence of the tool.

    ALL_TOOLS always carries recall_entity, so gating on that says yes to every
    agent including one that was never granted it.
    """
    assert _memory_section(["list_findings"]) == ""
    assert _memory_section(None) == ""
    assert _memory_section(["list_findings", "recall_entity"]) == _MEMORY_BLOCK


def test_render_base_prompt_includes_memory_only_for_a_granted_agent():
    without = render_base_prompt(role="Triage Agent", tools=["get_finding"])
    assert "<memory_operations>" not in without

    granted = render_base_prompt(
        role="Triage Agent", tools=["get_finding", "recall_entity"]
    )
    assert "<memory_operations>" in granted
    assert "recall_entity" in granted
    assert "read-only" in granted


def test_a_custom_agent_without_the_grant_is_not_told_to_recall():
    """#129 on a different tool: render_base_prompt is shared with custom agents,
    whose recommended_tools is user-supplied and carries no recall_entity, while
    _declare keeps only the names on that list.
    """
    from core.agents.manager import SOCAgentLibrary
    from core.llm.chat_layers import _declare

    row = {
        "id": "custom-1",
        "role": "Phishing Analyst",
        "recommended_tools": ["get_finding"],
    }
    profile = SOCAgentLibrary.build_profile(row)

    declared = {t["id"] for t in _declare(row["recommended_tools"], [])}
    assert "recall_entity" not in declared
    assert "recall_entity" not in profile.system_prompt


def test_a_custom_agent_granted_recall_is_told_about_it():
    from core.agents.manager import SOCAgentLibrary

    profile = SOCAgentLibrary.build_profile(
        {
            "id": "custom-2",
            "role": "Phishing Analyst",
            "recommended_tools": ["get_finding", "recall_entity"],
        }
    )
    assert "<memory_operations>" in profile.system_prompt


def test_builtin_principles_memory_lines_are_read_only():
    """Every builtin agent's extra_principles Memory line instructs read-only recall_entity."""
    forbidden_phrases = ["store FP reasoning"]
    for agent in BUILTIN_AGENTS:
        principles = agent.get("extra_principles", "")
        for phrase in forbidden_phrases:
            assert (
                phrase not in principles
            ), f"Agent {agent['id']} has '{phrase}' in extra_principles"

        # Every agent should have a Memory: principle directing recall_entity
        assert (
            "Memory: recall_entity" in principles
        ), f"Agent {agent['id']} missing 'Memory: recall_entity' in extra_principles"
        # Carries ADR 0015 where the model looks closest, not just the write ban:
        # "recall_entity ... before attributing" reads as memory feeding the
        # conclusion, which is the one thing recall may never do.
        assert (
            "read-only" in principles
        ), f"Agent {agent['id']} does not say memory is read-only"
        assert (
            "orients your search rather than deciding its outcome" in principles
        ), f"Agent {agent['id']} does not carry the ADR 0015 constraint"


@pytest.mark.parametrize(
    "workflow_name",
    ["incident-response", "full-investigation", "forensic-analysis", "cloud-incident"],
)
def test_compose_workflows_grant_recall_entity_in_all_phases(workflow_name: str):
    """All phases in compose workflows must include recall_entity in tools list (#735)."""
    workflow_path = (
        REPO / "core" / "workflows" / "definitions" / workflow_name / "WORKFLOW.md"
    )
    content = workflow_path.read_text()
    # Frontmatter is between the first two --- delimiters
    parts = content.split("---", 2)
    assert len(parts) >= 3, f"Invalid frontmatter in {workflow_name}"
    data = yaml.safe_load(parts[1])

    phases = data.get("phases", [])
    assert len(phases) > 0, f"No phases found in {workflow_name}"
    for phase in phases:
        tools = phase.get("tools", [])
        assert (
            "recall_entity" in tools
        ), f"Workflow '{workflow_name}' phase '{phase['id']}' missing recall_entity in tools: {tools}"


# Asks the real registry rather than a patched one, and fails loudly if this work
# is ever rebased somewhere #732 has not landed.
def test_recall_entity_is_registered_in_the_real_tool_registry():
    from core.llm.tool_schemas import ALL_TOOLS

    assert any(tool.get("name") == "recall_entity" for tool in ALL_TOOLS)


def test_the_prompt_names_only_things_the_tool_accepts():
    """The #129 defect in its smaller form, and it landed twice.

    A prompt naming a parameter or a key type that does not exist fails the way
    a prompt naming a missing tool does, but quieter: a bad key type is not an
    error, it is zero rows, which reads as an entity nobody has looked at
    (ADR 0016). The first draft of this block taught `sha256:`, which is not in
    ENTITY_KEY_TYPES, so every hash recall would have come back empty.
    """
    from core.memory.recall_contract import (
        ENTITY_KEY_TYPES,
        RECALL_ARGS,
        RECALL_KEY_ARGS,
    )

    words = {word.strip("`.,—") for word in _MEMORY_BLOCK.split()}

    assert words & set(RECALL_KEY_ARGS), "the block names no key argument"
    assert not {w for w in words if w.startswith("caller_")} - set(RECALL_ARGS)

    # The worked examples only. Backticked spans are the `type:value` shape
    # itself and the `sha256:` the block warns against, neither a real key.
    prose = re.sub(r"`[^`]*`", " ", _MEMORY_BLOCK)
    taught = set(re.findall(r"\b([a-z0-9_]+):\S", prose))
    unknown = taught - set(ENTITY_KEY_TYPES)
    assert not unknown, f"block teaches key types memory does not know: {unknown}"
    assert "hash" in taught, "the hash form is what the first draft got wrong"


def test_every_agents_recall_grant_survives_the_chat_declaration():
    """A grant that _declare drops is a prompt naming a tool the model cannot call.

    ``recommended_tools`` is a wish list; ``_declare`` is what the turn actually
    carries, and it keeps only the names it can resolve in ALL_TOOLS.
    """
    from core.llm.chat_layers import _declare

    for agent in BUILTIN_AGENTS:
        declared = {t["id"] for t in _declare(agent["recommended_tools"], [])}
        assert "recall_entity" in declared, f"{agent['id']} cannot call recall_entity"


# The acceptance criterion of #735, and the only one a structural check cannot
# reach: a triage agent asking about an entity that was ruled a false positive
# gets told so. Against a real Postgres, because the Verdict join is array
# containment and a fake would agree with whatever this module happened to do.
# Rows are seeded the way a Case closure writes them (#733) rather than by
# closing a Case, since what is under test here is the grant and the read.
@pytest.mark.database
@pytest.mark.external_service
@pytest.mark.asyncio
async def test_a_triage_agent_asking_about_a_false_positive_entity_receives_it(
    episodic_session,
):
    from datetime import datetime, timedelta, timezone

    from core.agents.tool_registry import execute_backend_tool
    from core.llm.chat_layers import _declare
    from core.storage.models import EpisodicVerdict, EpisodicVerdictSource

    key = "ip:10.2.3.4"
    concluded = datetime(2026, 8, 1, tzinfo=timezone.utc)

    triage = next(a for a in BUILTIN_AGENTS if a["id"] == AgentId.TRIAGE.value)
    assert "recall_entity" in {
        t["id"] for t in _declare(triage["recommended_tools"], [])
    }

    for run in range(3):
        row = EpisodicVerdict(
            investigation_kind="case",
            investigation_id=f"case-{run}",
            hypothesis_id=f"case-{run}",
            statement="10.2.3.4 is exfiltrating data",
            outcome="false_positive",
            rationale="scheduled backup traffic to the offsite target",
            subject_entities=[key],
            attacker_influenceable_only=False,
            trust="analyst",
            first_seen=concluded - timedelta(hours=2),
            last_seen=concluded,
            window_source="observed",
            concluded_at=concluded + timedelta(days=run),
        )
        episodic_session.add(row)
        episodic_session.flush()
        episodic_session.add(
            EpisodicVerdictSource(
                verdict_id=row.id,
                source_system="splunk",
                stance="supports",
                source_tier="telemetry",
            )
        )
    episodic_session.commit()

    result, handled = await execute_backend_tool(
        "recall_entity",
        {
            "entity_keys": [key],
            "caller_kind": "agent",
            "caller_id": AgentId.TRIAGE.value,
        },
    )

    assert handled is True
    outcomes = [v["outcome"] for v in result["verdicts"]]
    assert outcomes == ["false_positive"] * 3
    assert all(v["subject_entities"] == [key] for v in result["verdicts"])
    assert "backup" in result["verdicts"][0]["rationale"]
