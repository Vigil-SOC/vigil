# The arch asks for capabilities and Python binds them, so the two lists have to
# name the same things. Duplicated across the language boundary like RUN_KINDS,
# and held to it here rather than discovered when a worker silently loses a tool.

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Set

import pytest

from core.workflows.playbook_resolver import CAPABILITIES, HUNT_CAPABILITIES

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
ARCH = ROOT / "services" / "agent" / "arch" / "threathunt.yaml"
MCP_CONFIG = ROOT / "mcp-config.json"

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


def _declared_domains() -> List[str]:
    from core.workflows.workflows_service import WorkflowsService

    definition = WorkflowsService().get_workflow("threat-hunt")
    return list(definition.metadata.get("data_domains") or [])


def _needs_in_arch() -> set[str]:
    declared: set[str] = set()
    for line in ARCH.read_text().splitlines():
        match = re.match(r"\s*needs:\s*\[(.*)\]\s*$", line)
        if match:
            names = match.group(1).split(",")
            declared.update(one.strip() for one in names if one.strip())
    return declared


def test_the_arch_declares_needs_at_all():
    # A vacuous pass is the one way this check fails silently.
    assert _needs_in_arch(), "no role in threathunt.yaml declares needs:"


def test_python_binds_every_capability_the_arch_asks_for():
    unbound = _needs_in_arch() - set(HUNT_CAPABILITIES)
    assert not unbound, (
        "threathunt.yaml asks for capabilities the resolver does not emit, so the "
        f"roles needing them would be granted nothing: {sorted(unbound)}"
    )


def test_the_resolver_emits_nothing_the_arch_does_not_ask_for():
    unused = set(HUNT_CAPABILITIES) - _needs_in_arch()
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


# Corroboration is counted over distinct source systems, and a worker's schema is
# narrowed to this list at spec build. Declaring nothing leaves the field an open
# string; declaring the wrong things collapses every domain into one bucket.
def test_the_definition_declares_a_telemetry_vocabulary():
    assert (
        _declared_domains()
    ), "threat-hunt declares no data_domains, so source_system is unconstrained"


# Recorded runs show workers answering with their own agent id -- threat_hunter,
# network_analyst -- which the prompts forbid. If one of those were a declared
# domain, a worker would corroborate itself and two of its findings would read as
# two independent sources.
def test_no_declared_domain_is_the_name_of_a_worker():
    import yaml

    arch = yaml.safe_load(ARCH.read_text())
    overlap = set(_declared_domains()) & set(arch["roles"]["workers"])

    assert (
        not overlap
    ), f"these data_domains name a worker rather than a domain: {sorted(overlap)}"


# The definition's phases block is the roster of who the lead may dispatch, and the
# arch is what they actually are. A name in one and not the other reads as a
# worker that can be asked for and never answers.
def test_the_definition_rosters_the_workers_the_arch_carries():
    import yaml

    from core.workflows.workflows_service import WorkflowsService

    arch = yaml.safe_load(ARCH.read_text())
    rostered = WorkflowsService().get_workflow("threat-hunt").agents

    assert set(rostered) == set(arch["roles"]["workers"])


# The two sides state the per-turn call cost independently, and the agent layer
# raises max_calls by its own copy. If they drift, the resolver hands over a call
# ceiling the agent layer immediately overrides, and the number an operator sees
# in the config is not the one that governs the run.
def test_both_sides_agree_what_a_turn_costs_in_calls():
    from core.workflows.playbook_resolver import (
        CALLS_PER_ITERATION,
        DEFAULT_RUNTIME,
        HUNT_MAX_WORKERS,
    )

    types_ts = (
        ROOT / "services" / "agent" / "workflows" / "hunt" / "types.ts"
    ).read_text()

    # The formula, so a change to either side's arithmetic is a failure here and
    # not a number that quietly stops matching.
    formula = re.search(
        r"function callsPerIteration\(maxWorkers: number, "
        r"maxTurns: number\): number \{\s*"
        r"return \(maxWorkers \+ 2\) \* \(maxTurns \+ 2\);",
        types_ts,
    )
    assert formula is not None, "types.ts states a different per-turn formula"

    # And the inputs, which are the half that actually drifts: the arch's fan-out
    # and the runtime's turn cap live in files neither side reads from the other.
    stated = re.search(
        r"CALLS_PER_ITERATION = callsPerIteration\((\d+), (\d+)\)", types_ts
    )
    assert stated is not None, "types.ts no longer states CALLS_PER_ITERATION"
    workers, turns = int(stated.group(1)), int(stated.group(2))

    assert workers == HUNT_MAX_WORKERS
    assert turns == int(DEFAULT_RUNTIME["max_turns"])
    assert (workers + 2) * (turns + 2) == CALLS_PER_ITERATION


# The number the ratchet above pins is only right if it matches the arch a run is
# actually built from, which is a third file again.
def test_the_arch_fans_out_to_the_workers_the_budget_assumes():
    from core.workflows.playbook_resolver import HUNT_MAX_WORKERS

    arch = (ROOT / "services" / "agent" / "arch" / "threathunt.yaml").read_text()
    # Anchored: a comment a line above says "Serial is max_workers: 1", and an
    # unanchored search reads that instead of the setting.
    match = re.search(r"^\s+max_workers:\s*(\d+)", arch, re.MULTILINE)
    assert match is not None, "threathunt.yaml declares no max_workers"
    assert int(match.group(1)) == HUNT_MAX_WORKERS


# The cost ceiling is stated on both sides, and the agent layer derives the hard
# per-hunt limit from its own copy. A resolver that hands over more than the agent
# layer's default is refused at spec assembly -- which is how a raised ceiling
# turns into a run that never starts.
def test_both_sides_ship_the_same_cost_ceiling():
    from core.workflows.playbook_resolver import HUNT_BUDGETS

    types_ts = (
        ROOT / "services" / "agent" / "workflows" / "hunt" / "types.ts"
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
