# A built-in definition grants capabilities, which the resolver binds to whatever the
# deployment carries. A registry tool name in its place is bound by nobody, and an
# unknown capability binds to nothing, so both are caught here rather than in a run.
# Custom (user) workflows are data and resolve through compose, which grants tool
# names by design; they are not checked. A future compose-kind built-in that grants
# tools would need this rule revisited.

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest
import yaml

from core.llm.tool_schemas import ALL_TOOLS
from core.workflows.playbook_resolver import CAPABILITIES

pytestmark = pytest.mark.unit

DEFINITIONS = Path(__file__).resolve().parents[3] / "core" / "workflows" / "definitions"
TOOL_NAMES = {tool["name"] for tool in ALL_TOOLS if tool.get("name")}


def violations(definition: str, text: str) -> List[str]:
    """One ``<definition>/WORKFLOW.md:<line>: phase <id> ...: <name>`` per bad grant."""
    lines = text.split("\n")
    end = next(i for i, line in enumerate(lines[1:], 1) if line.startswith("---"))
    root = yaml.compose("\n".join(lines[1:end]))
    found = []
    for key, phases in root.value:
        if key.value != "phases":
            continue
        for phase in phases.value:
            fields = {k.value: v for k, v in phase.value}
            for tool in fields.get("tools", yaml.ScalarNode("", "")).value:
                name = tool.value
                # Capability first: case_records and get_finding are both.
                if name in CAPABILITIES:
                    continue
                kind = (
                    "names a tool, not a capability"
                    if name in TOOL_NAMES
                    else "names a capability the resolver does not know"
                )
                # +1 for the opening --- line, +1 for yaml's 0-based marks.
                where = f"{definition}/WORKFLOW.md:{tool.start_mark.line + 2}"
                phase_id = fields["id"].value if "id" in fields else "?"
                found.append(f"{where}: phase {phase_id} {kind}: {name}")
    return found


def _definition(tools: str) -> str:
    return (
        "---\nname: sample\nversion: 1\nphases:\n"
        "  - id: first\n    agent: first\n    tools: [telemetry_search]\n"
        f"  - id: second\n    agent: second\n    tools: [{tools}]\n---\nBody\n"
    )


def test_a_definition_that_names_capabilities_passes():
    assert violations("sample", _definition("entity_recall, case_records")) == []


def test_a_registry_tool_name_is_reported_with_definition_line_and_name():
    tool = next(name for name in sorted(TOOL_NAMES) if name not in CAPABILITIES)
    assert violations("sample", _definition(f"entity_recall, {tool}")) == [
        f"sample/WORKFLOW.md:10: phase second names a tool, not a capability: {tool}"
    ]


def test_an_unknown_capability_is_reported_with_definition_line_and_name():
    assert violations("sample", _definition("no_such_capability")) == [
        "sample/WORKFLOW.md:10: phase second names a capability the resolver does not know: "
        "no_such_capability"
    ]


def test_every_builtin_definition_grants_only_capabilities():
    paths = sorted(DEFINITIONS.glob("*/WORKFLOW.md"))
    assert paths
    found = [v for p in paths for v in violations(p.parent.name, p.read_text())]
    assert found == []
    # Guard against a vacuous pass: some built-in must grant phase tools.
    assert any("tools:" in p.read_text() for p in paths)
