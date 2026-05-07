"""Guard test: AVAILABLE_AGENTS in create_workflow.py must match AGENT_CONFIGS keys.

This ensures the list stays in sync as new agents are added to the SOC agent library.
See https://github.com/Vigil-SOC/vigil/issues/204
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from services.soc_agents import AGENT_CONFIGS  # noqa: E402

pytestmark = pytest.mark.unit


def _parse_available_agents() -> list[str]:
    """Extract AVAILABLE_AGENTS from create_workflow.py by parsing the source."""
    script_path = REPO / "scripts" / "create_workflow.py"
    script_source = script_path.read_text(encoding="utf-8")

    # Locate the AVAILABLE_AGENTS list definition
    start_marker = "AVAILABLE_AGENTS = ["
    start = script_source.find(start_marker)
    assert start != -1, "Could not find AVAILABLE_AGENTS list in create_workflow.py"

    # Find the matching closing bracket
    start = start + len(start_marker)
    depth = 1
    end = start
    while depth > 0 and end < len(script_source):
        if script_source[end] == "[":
            depth += 1
        elif script_source[end] == "]":
            depth -= 1
        if depth > 0:
            end += 1

    list_content = script_source[start:end]
    # Extract quoted strings
    agents = []
    for line in list_content.split("\n"):
        stripped = line.strip().rstrip(",")
        if stripped.startswith('"') and stripped.endswith('"'):
            agents.append(stripped[1:-1])
        elif stripped.startswith("'") and stripped.endswith("'"):
            agents.append(stripped[1:-1])
    return agents


def test_available_agents_matches_agent_configs():
    """AVAILABLE_AGENTS and AGENT_CONFIGS keys must be identical sets."""
    script_agents = set(_parse_available_agents())
    config_agents = set(AGENT_CONFIGS.keys())

    assert script_agents == config_agents, (
        f"Mismatch between scripts/create_workflow.py AVAILABLE_AGENTS "
        f"and services.soc_agents.AGENT_CONFIGS keys.\n"
        f"Only in script (should be removed): {script_agents - config_agents}\n"
        f"Only in AGENT_CONFIGS (should be added): {config_agents - script_agents}"
    )


def test_available_agents_has_no_duplicates():
    """AVAILABLE_AGENTS must not contain duplicate entries."""
    agents = _parse_available_agents()
    assert len(agents) == len(set(agents)), (
        f"Duplicate agents found in AVAILABLE_AGENTS: "
        f"{[a for a in agents if agents.count(a) > 1]}"
    )