# The arch asks for capabilities and Python binds them, so the two lists have to
# name the same things. Duplicated across the language boundary like RUN_KINDS,
# and held to it here rather than discovered when a worker silently loses a tool.

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Set

import pytest

from core.workflows.playbook_resolver import (
    CAPABILITIES,
    HUNT_CAPABILITIES,
    INVESTIGATE_CAPABILITIES,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
MCP_CONFIG = ROOT / "mcp-config.json"

# Every arch that drives the hunt loop, with the definition it is resolved for.
# Both resolve through resolve_hunt and are granted HUNT_CAPABILITIES, so both are
# what this ratchet is about. plugins/rootcause/plugin.yaml is not in this list: that trace binds
# telemetry_search on its own and does not ask the hunt for a roster. Not globbed:
# compose and chat are granted something else entirely, and investigate is held to
# INVESTIGATE_CAPABILITIES below.
HUNT_LIKE = [
    ("hunt/plugin.yaml", "threat-hunt"),
    ("adjudicate/plugin.yaml", "shadow-adjudication"),
]
ARCHES = [ROOT / "services" / "agent" / "plugins" / name for name, _ in HUNT_LIKE]
ARCH = ARCHES[0]

# Read off the source rather than imported: this must not need the mcp package
# installed, and the arch's needs: are parsed the same way below.
TOOL_NAME = re.compile(r'(?<![A-Za-z_])name="([A-Za-z0-9_]+)"')


# The servers this repo implements, as {server name: tool.py}. A third-party
# server has no module here, so its candidates cannot be checked this way.
def _in_repo_servers() -> Dict[str, Path]:
    servers = json.loads(MCP_CONFIG.read_text())["mcpServers"]
    found: Dict[str, Path] = {}
    for name, config in servers.items():
        if not isinstance(config, dict):
            continue
        for arg in config.get("args", []):
            if isinstance(arg, str) and arg.endswith("tool.py"):
                found[name] = ROOT / arg
    return found


def _tools_reported_by(path: Path) -> Set[str]:
    return set(TOOL_NAME.findall(path.read_text()))


def _declared_domains(workflow_id: str = "threat-hunt") -> List[str]:
    from core.workflows.workflows_service import WorkflowsService

    definition = WorkflowsService().get_workflow(workflow_id)
    return list(definition.metadata.get("data_domains") or [])


def _needs_in_arch(arch: Path = ARCH) -> set[str]:
    declared: set[str] = set()
    for line in arch.read_text().splitlines():
        match = re.match(r"\s*needs:\s*\[(.*)\]\s*$", line)
        if match:
            names = match.group(1).split(",")
            declared.update(one.strip() for one in names if one.strip())
    return declared


@pytest.mark.parametrize("arch", ARCHES, ids=lambda path: path.name)
def test_the_arch_declares_needs_at_all(arch):
    # A vacuous pass is the one way this check fails silently.
    assert _needs_in_arch(arch), f"no role in {arch.name} declares needs:"


@pytest.mark.parametrize("arch", ARCHES, ids=lambda path: path.name)
def test_python_binds_every_capability_the_arch_asks_for(arch):
    unbound = _needs_in_arch(arch) - set(HUNT_CAPABILITIES)
    assert not unbound, (
        f"{arch.name} asks for capabilities the resolver does not emit, so the "
        f"roles needing them would be granted nothing: {sorted(unbound)}"
    )


# The investigate lead's needs are granted from its own tuple, so each has to be a
# capability the resolver knows or the lead would silently hold nothing for it.
def test_every_investigate_need_is_a_capability_the_resolver_binds():
    arch = ROOT / "services" / "agent" / "plugins" / "investigate/plugin.yaml"
    needs = _needs_in_arch(arch)
    assert needs == set(INVESTIGATE_CAPABILITIES)
    assert needs <= set(CAPABILITIES)


# Over the union, not per arch: a capability only plugins/rootcause/plugin.yaml asks for is still
# one the resolver is right to bind, and checking each alone would call it unused.
def test_the_resolver_emits_nothing_any_hunt_like_arch_does_not_ask_for():
    asked = set().union(*(_needs_in_arch(arch) for arch in ARCHES))
    unused = set(HUNT_CAPABILITIES) - asked
    assert (
        not unused
    ), f"the resolver binds capabilities no role asks for: {sorted(unused)}"


def test_every_capability_names_at_least_one_candidate():
    empty = [name for name in HUNT_CAPABILITIES if not CAPABILITIES.get(name)]
    assert not empty, f"capabilities with no candidate tool: {empty}"


# The registry flattens to {server}_{tool}, and these servers already prefix their
# own names, so a candidate is only reachable if it names the tool the server
# actually reports. Guessing here is what left telemetry_search bound to nothing.
def test_in_repo_candidates_name_tools_their_server_reports():
    servers = _in_repo_servers()
    assert servers, "no in-repo MCP server found; the check would pass vacuously"

    unreachable = []
    for capability in HUNT_CAPABILITIES:
        for candidate in CAPABILITIES[capability]:
            path = servers.get(candidate.server or "")
            if path is None:
                continue
            reported = _tools_reported_by(path)
            unreachable += [
                f"{capability}: server {candidate.server} reports no tool {tool!r}"
                for tool in candidate.tools
                if tool not in reported
            ]

    assert not unreachable, (
        "candidates naming a tool their server does not report can never bind:\n"
        + "\n".join(unreachable)
    )


# The check above is only worth having if it covers the capability that was
# broken. A telemetry_search with no in-repo candidate passes it vacuously.
def test_telemetry_search_is_covered_by_an_in_repo_server():
    servers = _in_repo_servers()
    covered = [
        candidate.server
        for candidate in CAPABILITIES["telemetry_search"]
        if candidate.server in servers
    ]
    assert covered, "no telemetry_search candidate names a server this repo implements"


# A backend tool is not server-prefixed, so its candidate must resolve against
# the tool manifest instead. Same failure mode, different catalogue.
def test_backend_candidates_are_in_the_tool_manifest():
    from core.agents.tool_registry import MANIFEST

    missing = [
        f"{capability}: {tool}"
        for capability in HUNT_CAPABILITIES
        for candidate in CAPABILITIES[capability]
        if candidate.server is None
        for tool in candidate.tools
        if tool not in MANIFEST
    ]
    assert not missing, f"backend candidates absent from BACKEND_TOOLS: {missing}"


# Corroboration is counted over distinct source systems, and the finding schema is
# narrowed to this list at spec build. Declaring nothing leaves the field an open
# string; declaring the wrong things collapses every domain into one bucket.
@pytest.mark.parametrize("workflow_id", [wf for _, wf in HUNT_LIKE])
def test_the_definition_declares_a_telemetry_vocabulary(workflow_id):
    assert _declared_domains(
        workflow_id
    ), f"{workflow_id} declares no data_domains, so source_system is unconstrained"


# The allowance is stated three times: the plugin manifests a run is built from,
# the agent layer's fallback for a ledger journaled before the manifest stated it, and
# the resolver's max_calls. If they drift, the resolver hands over a call ceiling the
# agent layer immediately overrides, and a resumed hunt runs to different ceilings.
@pytest.mark.parametrize("arch_path", ARCHES, ids=lambda path: path.name)
def test_both_sides_agree_what_an_iteration_may_spend(arch_path):
    import yaml

    from core.workflows.playbook_resolver import CALLS_PER_ITERATION

    allowance = yaml.safe_load(arch_path.read_text())["allowance"]
    types_ts = (
        ROOT / "services" / "agent" / "plugins" / "hunt" / "types.ts"
    ).read_text()
    stated = re.search(
        r"HUNT_ALLOWANCE: Allowance = \{ turns_per_iteration: (\d+), "
        r"calls_per_iteration: (\d+) \}",
        types_ts,
    )
    assert stated is not None, "types.ts no longer states HUNT_ALLOWANCE"

    # The numbers the retired fan-out arithmetic produced: 4 workers plus the lead
    # and the critic, each with 8 turns and 2 emission retries.
    assert allowance == {"turns_per_iteration": 6, "calls_per_iteration": 60}
    assert (int(stated.group(1)), int(stated.group(2))) == (6, 60)
    assert allowance["calls_per_iteration"] == CALLS_PER_ITERATION


# The cost ceiling is stated on both sides, and the agent layer derives the hard
# per-hunt limit from its own copy. A resolver that hands over more than the agent
# layer's default is refused at spec assembly -- which is how a raised ceiling
# turns into a run that never starts.
def test_both_sides_ship_the_same_cost_ceiling():
    from core.workflows.playbook_resolver import HUNT_BUDGETS

    types_ts = (
        ROOT / "services" / "agent" / "plugins" / "hunt" / "types.ts"
    ).read_text()
    stated = re.search(r"max_cost_usd: ([\d.]+),", types_ts)
    assert stated is not None, "types.ts no longer states max_cost_usd"
    assert float(stated.group(1)) == HUNT_BUDGETS["max_cost_usd"]


# A turn count that the call meter trips first is a number nothing enforces --
# which is the bug this pair replaced, where 24 calls ended a hunt at turn 2.
def test_the_call_ceiling_leaves_room_for_the_turns_it_ships_with():
    from core.workflows.playbook_resolver import HUNT_BUDGETS, HUNT_THRESHOLDS

    assert (
        HUNT_BUDGETS["max_calls"] >= HUNT_THRESHOLDS["max_iterations"] * 2
    ), "max_calls would end the hunt before it spent its turns"
