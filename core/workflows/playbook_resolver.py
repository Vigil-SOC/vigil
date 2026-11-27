# Resolves a Playbook reference into the two layers the agent layer parses. The
# reference is what the job carries; this is where it becomes a run's content.

from __future__ import annotations

import copy
import logging
import math
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

import yaml

from core.agents.enablement import disabled_agent_ids, disabled_message
from core.agents.run_limits import MAX_COST_USD, MAX_ITERATIONS
from core.config import get_settings
from core.integrations import bindings
from core.llm.defaults import DEFAULT_MODEL
from core.response.ceiling import IRREVERSIBLE, REVERSIBLE, gated_tools, tool_classes

if TYPE_CHECKING:
    from core.integrations.mcp.registry import MCPRegistry
    from core.workflows.workflows_service import WorkflowDefinition, WorkflowsService

logger = logging.getLogger(__name__)


# Named for what the agent layer already defaults to, so a resolved config that
# states nothing unusual states the same numbers a packaged one would. Replay
# reads this constant. Investigate and compose runs overlay max_wall_ms from
# WORKFLOW_DEFAULT_WALL_SECONDS at resolve time; this dict stays 30 minutes.
DEFAULT_SPEND = {"max_cost_usd": 5.0, "max_wall_ms": 1_800_000}
# Seconds. Below a minute is not a wall an operator would set on purpose, and a
# day is the override ceiling. Outside this, the setting is ignored.
WALL_SECONDS_MIN = 60
WALL_SECONDS_MAX = 86_400
WALL_SETTING = "WORKFLOW_DEFAULT_WALL_SECONDS"
DEFAULT_RUNTIME = {"max_turns": 8, "result_cap": 20_000, "recall_limit": 3}


def deployment_wall_ms() -> int:
    """Wall for an investigate or compose run whose start names none.

    Read when the run starts. A number outside 60–86400 seconds is ignored and
    the 30-minute constant stands; a non-numeric value never reaches here.
    """
    seconds = get_settings().workflow_default_wall_seconds
    if WALL_SECONDS_MIN <= seconds <= WALL_SECONDS_MAX:
        return seconds * 1000
    return DEFAULT_SPEND["max_wall_ms"]


def wall_setting_warning() -> Optional[str]:
    """Why the deployment wall was ignored, or None when the setting is in range."""
    seconds = get_settings().workflow_default_wall_seconds
    if WALL_SECONDS_MIN <= seconds <= WALL_SECONDS_MAX:
        return None
    return (
        f"{WALL_SETTING} is {seconds}, outside {WALL_SECONDS_MIN}-{WALL_SECONDS_MAX} "
        "seconds; using 1800 seconds"
    )


def warn_default_wall() -> None:
    """Log once, at API start, when the deployment wall is a number out of range."""
    message = wall_setting_warning()
    if message:
        logger.warning("%s", message)


def _with_deployment_wall(budgets: Dict[str, Any]) -> Dict[str, Any]:
    return {**budgets, "max_wall_ms": deployment_wall_ms()}


class UnknownPlaybook(Exception):
    pass


# Two attempts at the answer follow a phase's tool loop, so a phase costs at most
# this many model calls.
EMIT_ATTEMPTS = 2

REMOTE = "remote"
EMULATION = bindings.EMULATION

# What the investigate arch's lead asks for. Only telemetry_search needs an
# integration; the rest are native tools. Naming them as capabilities is what lets
# a missing one reach the run as a blind spot rather than a log line.
INVESTIGATE_CAPABILITIES = (
    "shell",
    "entity_recall",
    "get_finding",
    "telemetry_search",
    "findings_search",
    "indicator_lookup",
    # OpenCTI's graph, held when it is connected (OPTIONAL_CAPABILITIES).
    "intel_observable",
    "intel_indicator",
    "intel_entities",
    "intel_relationships",
)


def _tool_catalogue(registry: Optional["MCPRegistry"]) -> Dict[str, Dict[str, Any]]:
    from core.cases.case_view import shell_address
    from core.llm.tool_schemas import ALL_TOOLS

    catalogue = {tool["name"]: tool for tool in ALL_TOOLS if tool.get("name")}
    # No sidecar here (Helm, until #4200): the shell binds nothing, and the run
    # reports it as a blind spot rather than holding a tool that always fails.
    if shell_address() == (None, None):
        catalogue.pop("read_case_shell", None)
    # The integrations this deployment carries, as tools with the same shape. A
    # server that is not connected reports nothing, so it binds nothing.
    for tool in _mcp_catalogue(registry):
        catalogue.setdefault(tool["name"], tool)
    return catalogue


def _mcp_catalogue(registry: Optional["MCPRegistry"]) -> List[Dict[str, Any]]:
    if registry is None:
        return []
    # Sync to the running client's live connection state first, so a server
    # enabled+connected after boot binds here without a reload. No-op when there
    # is no process client. Best effort in its own right: a refresh that fails
    # must not cost us the registry we already hold, nor the cache fallback below.
    try:
        from core.integrations.mcp.registry import refresh_from_client

        refresh_from_client(registry)
    except Exception as exc:  # noqa: BLE001
        logger.warning("MCP refresh failed while resolving tools: %s", exc)

    try:
        tools = registry.get_all_tools()
        # An empty registry and a deployment with no integrations resolve differently,
        # so fill from the cache before concluding a capability binds to nothing.
        if not tools:
            from core.integrations.mcp.registry import populate_from_cache

            if populate_from_cache(registry):
                tools = registry.get_all_tools()
        return tools
    except Exception as exc:  # noqa: BLE001
        logger.warning("MCP registry unavailable while resolving tools: %s", exc)
        return []


# What an arch means when it asks for a capability: every tool bound to it
# (core/integrations/bindings.py) that this deployment carries answers, and the
# agent picks between them; none present drops the capability, which the hunt
# journals as a visibility gap rather than running blind.
CAPABILITY_NAMES = bindings.CAPABILITY_NAMES

# A need a stored manifest may still name, and the capability it now means.
NEED_ALIASES: Dict[str, str] = {"atomic_red_team_execute": EMULATION}


def candidates_for(capability: str) -> List[Tuple[Optional[str], str]]:
    """(server, catalogue name) of each tool answering a capability; a need that
    is none names one catalogue tool."""
    if capability in CAPABILITY_NAMES:
        return [(b.server, b.tool) for b in bindings.tools_for(capability)]
    return [(None, capability)]


# What one call of a capability may take and return, decided by the capability rather
# than the server: a telemetry search scans a corpus, an indicator lookup reads a row.
# The agent layer's 30s default is not a SIEM timeout, and a search that crosses it
# reaches the hunt as a gap in visibility rather than as a search that was cut off.
CAPABILITY_BOUNDS: Dict[str, Dict[str, int]] = {
    "telemetry_search": {"timeout_ms": 120_000, "max_rows": 500},
    "indicator_lookup": {"timeout_ms": 60_000, "max_rows": 200},
    "findings_search": {"timeout_ms": 30_000, "max_rows": 200},
    # A read of our own Postgres, so the findings timeout is generous. The row
    # cap is stated because the bounds model requires one, not because it bites:
    # recall answers with one envelope, and the injected `limit` is in
    # RECALL_IGNORED_ARGS because memory caps each list itself.
    "entity_recall": {"timeout_ms": 30_000, "max_rows": 200},
    # A first render extracts every attachment (PARSE_TIMEOUT_S, 60s, in
    # parallel), then the command runs to the sidecar's own 30s limit. One
    # answer, so the row cap is only the bounds model's shape.
    "shell": {"timeout_ms": 120_000, "max_rows": 1},
    # One row of our own Postgres. Its size is the record behind the alert, which
    # result_cap bounds, so the row cap is only the shape the bounds model asks for.
    "get_finding": {"timeout_ms": 15_000, "max_rows": 1},
    # A read of our own Postgres, one answer per technique asked; the row cap is
    # only the bounds model's shape.
    "detection_coverage": {"timeout_ms": 30_000, "max_rows": 20},
    # A save proves the rule on the SIEM, then a gaps run's save lints and replays
    # its proposal (tools.GAP_REPLAY_TIMEOUT_S): the 30s default would cut the
    # answer off after the rule is saved.
    "save_detection_rule": {"timeout_ms": 240_000, "max_rows": 200},
    # Each tool caps its own list at 50; this is the shape the bounds model asks for.
    **{
        name: {"timeout_ms": 60_000, "max_rows": 50}
        for name in (
            "intel_observable",
            "intel_indicator",
            "intel_entities",
            "intel_relationships",
        )
    },
    # One account from an external API; the row cap allows a short user search.
    "identity_lookup": {"timeout_ms": 30_000, "max_rows": 20},
    # A sandbox API read of one hash or one report; the row cap is the bounds
    # model's shape.
    "sandbox_hash_lookup": {"timeout_ms": 60_000, "max_rows": 50},
    "sandbox_report": {"timeout_ms": 60_000, "max_rows": 200},
    # A topology read, and a console nudge that answers with an acknowledgement.
    "topology_graph": {"timeout_ms": 60_000, "max_rows": 200},
    "topology_focus": {"timeout_ms": 30_000, "max_rows": 20},
    "topology_legend": {"timeout_ms": 30_000, "max_rows": 20},
    # An emulator runs one technique on one host; a runner's own limit is 180s.
    # Its answer is a step or a few; the row cap is the bounds model's shape.
    EMULATION: {"timeout_ms": 240_000, "max_rows": 200},
}


# The tools that answer each capability the arch asked for, bound to what this
# deployment reports: every bound tool present, so the agent picks between them.
# provides is what the agent layer matches a role's needs on. A need that is not
# a capability but names a catalogue tool binds that tool.
def _bound_capabilities(
    needs: List[str], catalogue: Dict[str, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    bound: List[Dict[str, Any]] = []
    for need in needs:
        capability = NEED_ALIASES.get(need, need)
        found = False
        for server, name in candidates_for(capability):
            if name not in catalogue:
                continue
            found = True
            entry = catalogue[name]
            bound.append(
                {
                    "id": name,
                    "kind": REMOTE,
                    "provides": capability,
                    "description": entry.get("description", ""),
                    "parameters": entry.get("input_schema") or {},
                    # The integration behind it, so a hunt can name the system a
                    # row came from. A tool this backend implements names none.
                    **({"server": server} if server else {}),
                    **CAPABILITY_BOUNDS.get(capability, {}),
                }
            )
        if not found:
            # An opt-in integration that is not connected is the expected case.
            (logger.debug if capability in OPTIONAL_CAPABILITIES else logger.warning)(
                "no tool in this deployment provides %s; roles needing it lose it",
                capability,
            )
    return bound


# The needs an operator manifest may not hold: neither a capability nor a tool
# this deployment carries. Empty when every need binds or can bind.
def unknown_needs(
    needs: List[str], registry: Optional["MCPRegistry"] = None
) -> List[str]:
    catalogue = _tool_catalogue(registry)
    return [
        n
        for n in needs
        if n not in CAPABILITY_NAMES and n not in NEED_ALIASES and n not in catalogue
    ]


# What an investigate run's lead holds: an operator manifest's needs, else the
# investigate plugin's.
def lead_needs(definition: Any) -> List[str]:
    needs = definition.metadata.get("needs")
    return list(needs) if isinstance(needs, list) else list(INVESTIGATE_CAPABILITIES)


# An agent's prompt is rendered now rather than read from a file: the memory block
# depends on the agent's own grant, so a stored copy would describe another agent.
def _profile_for(agent_id: str) -> Any:
    from core.agents.manager import (
        CUSTOM_AGENT_ID_PREFIX,
        AgentManager,
        SOCAgentLibrary,
    )

    # Built-in hit: catalog, no I/O. custom- miss: AgentManager refreshes from
    # the DB in __init__ — the same seam as routers.agents._resolve_agent.
    # SOCAgentLibrary stays the builtins catalog.
    profile = SOCAgentLibrary.get_agent(agent_id)
    if profile is None and agent_id and agent_id.startswith(CUSTOM_AGENT_ID_PREFIX):
        profile = AgentManager().agents.get(agent_id)
    if profile is None:
        raise UnknownPlaybook(f"phase names agent {agent_id}, which does not exist")
    if agent_id in disabled_agent_ids():
        raise UnknownPlaybook(f"phase names {disabled_message(agent_id)}")
    return profile


# Phase agents that are turned off, in phase order. One read of the switch for the
# whole definition.
# A caller checking many definitions passes the set it already read.
def disabled_phase_agents(
    definition: Any, disabled: Optional[Set[str]] = None
) -> List[str]:
    if disabled is None:
        disabled = disabled_agent_ids()
    named = (
        (phase or {}).get("agent") or (phase or {}).get("agent_id")
        for phase in definition.phases
    )
    return [agent for agent in dict.fromkeys(named) if agent in disabled]


def _prompt_for(agent_id: str) -> str:
    return _profile_for(agent_id).system_prompt


# What each phase's agent needs, in phase order, as its profile states them; nothing
# for an agent that does not exist. A listing reads this, so custom agents load
# once for all the phases and the on/off switch is not read: a run checks that.
def phases_needs(phases: List[Dict[str, Any]]) -> List[List[str]]:
    from core.agents.manager import (
        CUSTOM_AGENT_ID_PREFIX,
        AgentManager,
        SOCAgentLibrary,
    )

    custom: Optional[Dict[str, Any]] = None
    needs: List[List[str]] = []
    for phase in phases:
        agent = (phase or {}).get("agent") or (phase or {}).get("agent_id") or ""
        profile = SOCAgentLibrary.get_agent(agent)
        if profile is None and agent.startswith(CUSTOM_AGENT_ID_PREFIX):
            if custom is None:
                custom = AgentManager().agents
            profile = custom.get(agent)
        needs.append(list(profile.recommended_tools or []) if profile else [])
    return needs


# A file playbook writes one instructions block. A custom workflow authors the same
# thing as purpose, numbered steps and an expected output, and all three are it.
def _instructions_of(phase: Dict[str, Any]) -> str:
    stated = (phase.get("instructions") or "").strip()
    if stated:
        return stated

    parts = [(phase.get("purpose") or "").strip()]
    steps = [str(s).strip() for s in (phase.get("steps") or []) if str(s).strip()]
    if steps:
        parts.append("\n".join(f"{i}. {s}" for i, s in enumerate(steps, start=1)))
    expected = (phase.get("expected_output") or "").strip()
    if expected:
        parts.append(f"Expected output: {expected}")

    return "\n\n".join(part for part in parts if part)


# Every phase holds its agent's needs as this deployment binds them; without a
# catalogue (a preflight read) the needs themselves. A need that binds nothing is
# returned beside them, so the run journals a blind spot rather than a log line; an
# optional capability nothing answers is an opt-in not connected, and records none.
def _phase_tools(
    profile: Any, catalogue: Optional[Dict[str, Dict[str, Any]]]
) -> Tuple[List[str], List[Dict[str, str]]]:
    needs = list(profile.recommended_tools or [])
    if catalogue is None:
        return needs, []
    tools: List[str] = []
    unavailable: List[Dict[str, str]] = []
    for need in needs:
        bound = [t["id"] for t in _bound_capabilities([need], catalogue)]
        if not bound and NEED_ALIASES.get(need, need) not in OPTIONAL_CAPABILITIES:
            unavailable.append(
                {"tool": need, "reason": f"no tool in this deployment answers {need}"}
            )
        tools += [t for t in bound if t not in tools]
    return tools, unavailable


def _phases_of(
    definition: Any, catalogue: Optional[Dict[str, Dict[str, Any]]] = None
) -> List[Dict[str, Any]]:
    resolved: List[Dict[str, Any]] = []
    for index, phase in enumerate(definition.phases):
        phase = phase or {}
        agent = phase.get("agent") or phase.get("agent_id") or ""
        if not agent:
            raise UnknownPlaybook(f"phase {index + 1} names no agent")

        profile = _profile_for(agent)
        tools, unavailable = _phase_tools(profile, catalogue)
        resolved.append(
            {
                "id": phase.get("id") or phase.get("phase_id") or f"phase-{index + 1}",
                "agent": agent,
                "name": phase.get("name") or f"Phase {index + 1}",
                "instructions": _instructions_of(phase),
                "approval_required": bool(phase.get("approval_required")),
                "tools": tools,
                "prompt": profile.system_prompt,
                **({"unavailable": unavailable} if unavailable else {}),
            }
        )
    return resolved


# The tools the phases name, as this deployment carries them. A catalogue handed
# to the registry would widen every grant to everything, which is deny-by-default
# inverted. Phases hold only bound ids; _phase_tools reports the needs that bound none.
def _tools_of(
    phases: List[Dict[str, Any]], catalogue: Dict[str, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    wanted: List[str] = []
    for phase in phases:
        for tool in phase["tools"]:
            if tool not in wanted:
                wanted.append(tool)

    tools: List[Dict[str, Any]] = []
    for name in wanted:
        entry = catalogue.get(name)
        if entry is None:
            # Dropped rather than fatal: a playbook naming a tool this deployment
            # does not carry should lose that tool, not fail to run at all.
            logger.warning("playbook names unknown tool %s; dropping it", name)
            continue
        tools.append(
            {
                "id": name,
                "kind": REMOTE,
                "description": entry.get("description", ""),
                "parameters": entry.get("input_schema") or {},
                **CAPABILITY_BOUNDS.get(name, {}),
            }
        )
    return tools


# Derived, not a constant: max_turns bounds a phase and this bounds the run, so a
# flat ceiling would starve a long playbook part-way through. Cost and wall bind.
def _budgets(phases: List[Dict[str, Any]]) -> Dict[str, Any]:
    per_phase = int(DEFAULT_RUNTIME["max_turns"]) + EMIT_ATTEMPTS
    return _with_deployment_wall(
        {"max_calls": max(len(phases), 1) * per_phase, **DEFAULT_SPEND}
    )


# Every outside tool whose stored class is reversible, irreversible, or none.
# The id must be one config.tools actually carries: spec.ts refuses an approvals
# name that is not declared. A phase that lists one parks on that call,
# arguments and all, until someone who may approve releases it. Chat never
# grants these; a workflow must not grant them ungated either.
def _approvals_of(tools: List[Dict[str, Any]]) -> List[str]:
    gated = gated_tools(tool["id"] for tool in tools)
    return [tool["id"] for tool in tools if tool["id"] in gated]


# The tools that change something when they succeed: one on Vigil's MCP server not
# hinted read-only, else a built-in whose permission is more than a read (the hint
# is the finer word: approve_action is gated as a decision but only reads), and an
# outside one stored as reversible or irreversible. The agent layer reads the mark
# at the run's end, so an answer that claims a save whose call failed is followed
# by Vigil's own line saying so. An unclassified outside tool is not marked: it may
# be a read.
def _mark_writes(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from core.agents.tool_registry import TOOL_PERMISSIONS
    from core.integrations.mcp import in_process

    names = [tool["id"] for tool in tools]
    own = {
        name
        for name in names
        if (hint := in_process.changes(name)) is True
        or (
            hint is None
            and name in TOOL_PERMISSIONS
            and not TOOL_PERMISSIONS[name].endswith(".read")
        )
    }
    outside = {
        name
        for name, effect in tool_classes(names).items()
        if effect in (REVERSIBLE, IRREVERSIBLE)
    }
    writes = own | outside
    # A tool bound as an emulator is one wherever it was bound: the agent layer
    # journals each call to it, and it gets the emulation bounds.
    from core.response.emulation import read_emulation

    marked = bindings.emulators()
    emulation = read_emulation() if marked & set(names) else None
    return [
        {
            **tool,
            **({"writes": True} if tool["id"] in writes else {}),
            **(
                {
                    **CAPABILITY_BOUNDS[EMULATION],
                    "emulator": True,
                    **_name_hosts(tool, emulation.hosts),
                }
                if emulation is not None and tool["id"] in marked
                else {}
            ),
        }
        for tool in tools
    ]


# An emulator's hostname is narrowed to the listed hosts, and its description
# names them, so the model asks only for a host it can run on. tools_router
# still refuses an unlisted host: the list may change mid-run. The schema is
# copied first; the catalogue entry is shared between runs.
def _name_hosts(tool: Dict[str, Any], hosts: Tuple[str, ...]) -> Dict[str, Any]:
    from core.response.emulation import LIMITS_POINTER

    parameters = copy.deepcopy(tool.get("parameters") or {})
    hostname = (parameters.get("properties") or {}).get("hostname")
    if not isinstance(hostname, dict):
        return {}
    if hosts:
        hostname["enum"] = list(hosts)
        note = f"Runs only on the hosts listed for emulation: {', '.join(hosts)}."
    else:
        note = (
            "No host is listed for emulation, so every call is refused until "
            f"an operator lists one under {LIMITS_POINTER}."
        )
    description = (tool.get("description") or "").rstrip()
    return {
        "parameters": parameters,
        "description": f"{description}\n\n{note}" if description else note,
    }


def resolve(
    workflow_id: str,
    model: Optional[str] = None,
    workflows: Optional["WorkflowsService"] = None,
    registry: Optional["MCPRegistry"] = None,
    provider: Optional[str] = None,
    effort: Optional[str] = None,
) -> Tuple[str, str]:
    """Return the playbook and config layers for ``workflow_id``, as YAML text."""
    from core.workflows.workflows_service import WorkflowsService

    definition = (workflows or WorkflowsService()).get_workflow(workflow_id)
    if definition is None:
        raise UnknownPlaybook(f"no such workflow: {workflow_id}")

    catalogue = _tool_catalogue(registry)
    phases = _phases_of(definition, catalogue)
    # A lead has no steps; its playbook is the objectives and the body. Compose
    # with none completes instantly having done nothing, which reads exactly
    # like a run that worked.
    if not phases and definition.run_kind != "investigate":
        raise UnknownPlaybook(
            f"{workflow_id} declares no phases; there is nothing to run"
        )

    # A lead holds what it asked for; compose grants per phase and has no lead.
    lead = (
        _bound_capabilities(lead_needs(definition), catalogue)
        if definition.run_kind == "investigate"
        else []
    )
    tools = _mark_writes(
        lead
        + [
            tool
            for tool in _tools_of(phases, catalogue)
            if tool["id"] not in {held["id"] for held in lead}
        ]
    )

    playbook = {
        "name": definition.name,
        "description": definition.description,
        "use_case": definition.use_case,
        "trigger_examples": list(definition.trigger_examples),
        "objectives": list(definition.metadata.get("objectives") or []),
        "scope": dict(definition.metadata.get("scope") or {}),
        "directives": dict(definition.metadata.get("directives") or {}),
        "phases": phases,
        "narrative": definition.body,
    }

    config = {
        "model": model or DEFAULT_MODEL,
        **({"provider": provider} if provider else {}),
        **({"effort": effort} if effort else {}),
        **_instructions(),
        "budgets": (
            _with_deployment_wall(dict(INVESTIGATE_BUDGETS))
            if definition.run_kind == "investigate"
            else _budgets(phases)
        ),
        "runtime": DEFAULT_RUNTIME,
        "tools": tools,
        # Direct-action tools park until a person approves the call. A phase
        # checkpoint (approval_required on the step) is a separate gate and is
        # not a substitute: it does not name the call or its arguments.
        "approvals": _approvals_of(tools),
        "thresholds": {},
    }

    return _dump(playbook), _dump(config)


# What the threathunt arch asks for, by capability. Duplicated across the language
# boundary for the same reason RUN_KINDS is, and must match it.
HUNT_CAPABILITIES = (
    "findings_search",
    # One alert's whole record, its raw source evidence included. A page of findings
    # names each alert's hosts and users; this is for the few worth reading in full.
    "get_finding",
    "telemetry_search",
    "indicator_lookup",
    # Bound on the run rather than granted to every role: `needs` in the arch is
    # what decides who holds it, and the lead and critic are not granted it.
    "entity_recall",
    "detection_coverage",
    "intel_observable",
    "intel_indicator",
    "intel_entities",
    "intel_relationships",
    # The account a hunt is about, from the identity provider when one is connected.
    "identity_lookup",
)

# Needs a run of any kind holds when the deployment answers them and goes without
# otherwise, with no blind spot journalled: an opt-in integration that is not
# connected, or a coverage read, says nothing about this estate's visibility.
# Mirrors `optional` in the hunt and investigate plugin.yaml.
OPTIONAL_CAPABILITIES = (
    "detection_coverage",
    "intel_observable",
    "intel_indicator",
    "intel_entities",
    "intel_relationships",
    # No identity provider is no blind spot on the estate: the account's existence
    # goes to open questions instead (the insider-threat skill).
    "identity_lookup",
    # Sandboxes and a topology integration are opt-in.
    "sandbox_hash_lookup",
    "sandbox_report",
    "topology_graph",
    "topology_focus",
    "topology_legend",
)

# A hunt is bounded by iterations rather than phases. Separate ceilings: max_calls
# only backstops a runaway turn, and the agent layer raises it to match the turn
# count a run asks for.
HUNT_ITERATIONS = 8

# The model calls one iteration may spend: the hunt plugin's stated allowance
# (services/agent/plugins/hunt/plugin.yaml).
CALLS_PER_ITERATION = 60
# 90 minutes of wall: at several minutes an iteration, a half-hour ceiling ends the
# hunt on the clock with most of its cost budget unspent. The shipped hunt limits;
# a hunt runs on hunt_limits(), which a deployment may raise. Root-cause traces
# borrow cost and wall from here and stay on these.
HUNT_BUDGETS = {
    "max_calls": HUNT_ITERATIONS * CALLS_PER_ITERATION,
    "max_cost_usd": 15.0,
    "max_wall_ms": 5_400_000,
}

# The deployment's hunt defaults (#5333): setting, env name, the range it may take
# (and that range in words), and the shipped value it falls back to. Bounded by the
# per-run override ceilings, so a default never grants more than a person may ask
# for on execute.
_HUNT_SETTINGS = (
    (
        "hunt_default_iterations",
        "HUNT_DEFAULT_ITERATIONS",
        1,
        MAX_ITERATIONS,
        f"1-{MAX_ITERATIONS}",
        HUNT_ITERATIONS,
    ),
    # Cost has no floor but being positive; 0 itself is refused below.
    (
        "hunt_default_cost_usd",
        "HUNT_DEFAULT_COST_USD",
        0,
        MAX_COST_USD,
        f"above 0 and at most {MAX_COST_USD:g} USD",
        HUNT_BUDGETS["max_cost_usd"],
    ),
    (
        "hunt_default_wall_seconds",
        "HUNT_DEFAULT_WALL_SECONDS",
        WALL_SECONDS_MIN,
        WALL_SECONDS_MAX,
        f"{WALL_SECONDS_MIN}-{WALL_SECONDS_MAX} seconds",
        HUNT_BUDGETS["max_wall_ms"] // 1000,
    ),
)


def _hunt_setting(value: float, low: float, high: float) -> Optional[float]:
    """The setting's value, or None when it is outside its range."""
    if math.isfinite(value) and 0 < value and low <= value <= high:
        return value
    return None


def hunt_limits() -> Tuple[int, float, int]:
    """(iterations, cost USD, wall ms) for a hunt whose start names none.

    Read when the run starts and by the preflight, so the run form shows what runs.
    A setting out of range keeps the shipped value.
    """
    settings = get_settings()
    iterations, cost, wall_seconds = (
        shipped if (v := _hunt_setting(getattr(settings, f), lo, hi)) is None else v
        for f, _, lo, hi, _, shipped in _HUNT_SETTINGS
    )
    return int(iterations), float(cost), int(wall_seconds) * 1000


def hunt_setting_warning() -> Optional[str]:
    """Why a deployment hunt default was ignored, or None when all are in range."""
    settings = get_settings()
    ignored = [
        f"{env} is {getattr(settings, f):g}, not {words}; using {shipped:g}"
        for f, env, lo, hi, words, shipped in _HUNT_SETTINGS
        if _hunt_setting(getattr(settings, f), lo, hi) is None
    ]
    return "; ".join(ignored) or None


def warn_hunt_defaults() -> None:
    """Log once, at API start, when a deployment hunt default is out of range."""
    message = hunt_setting_warning()
    if message:
        logger.warning("%s", message)


def _hunt_config_limits() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """A hunt config's budgets and thresholds, from the deployment defaults."""
    iterations, cost, wall_ms = hunt_limits()
    budgets = {
        "max_calls": iterations * CALLS_PER_ITERATION,
        "max_cost_usd": cost,
        "max_wall_ms": wall_ms,
    }
    return budgets, {**HUNT_THRESHOLDS, "max_iterations": iterations}


# What a root-cause trace needs from the deployment. record and finish are the
# trace's own tools; telemetry_search is the one an integration (a SIEM) answers, and
# a handoff starts no trace the deployment cannot serve (run_bridge_router).
ROOT_CAUSE_CAPABILITIES = ("telemetry_search",)

# Above the harness defaults (8 turns, 12 calls) so the $15 and 90-minute ceilings
# are what stop a trace, not the tool-turn cap.
ROOT_CAUSE_MAX_TURNS = 1024
ROOT_CAUSE_MAX_CALLS = 1024

# Under thresholds rather than budgets: the agent layer's budget block refuses added
# keys, and a turn is the hunt's unit rather than the harness's.
HUNT_THRESHOLDS = {"max_iterations": HUNT_ITERATIONS}

# An investigation has no phases to count: its lead decides until it concludes, so
# it gets as many decisions as a hunt gets iterations, each with a full tool loop.
INVESTIGATE_BUDGETS = {
    "max_calls": HUNT_ITERATIONS * (int(DEFAULT_RUNTIME["max_turns"]) + EMIT_ATTEMPTS),
    **DEFAULT_SPEND,
}


# What this deployment can and cannot answer, without resolving a whole playbook. The
# console asks before a run starts, so the deployment gap is told before the spend.
# An optional need nothing answers is absent, not unbound: the run goes without it
# and records no gap.
def capability_report(
    registry: Optional["MCPRegistry"] = None,
    needs: Tuple[str, ...] = HUNT_CAPABILITIES,
) -> Dict[str, List[str]]:
    catalogue = _tool_catalogue(registry)
    bound = {
        tool["provides"]
        for tool in _bound_capabilities(list(needs), catalogue)
        if tool.get("provides")
    }
    missing = [name for name in needs if name not in bound]
    return {
        "bound": [name for name in needs if name in bound],
        "unbound": [n for n in missing if n not in OPTIONAL_CAPABILITIES],
        "absent": [n for n in missing if n in OPTIONAL_CAPABILITIES],
    }


# The null hypothesis on the board from the start. Without it the benign
# explanation is only ever an objection, never a competing claim -- which is the
# whole protection against a loop that searches until it finds something.
HUNT_HYPOTHESIS_LOOP = True


def _strings(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value or [] if str(item).strip()]


def _site_telemetry() -> List[Dict[str, str]]:
    """``{name, covers}`` of each telemetry entry the applied site declares."""
    from core.site import applied_site, declared

    row = applied_site()
    if not declared(row):
        return []
    return [
        {"name": t["name"], "covers": t["covers"]}
        for t in (row.get("items") or {}).get("telemetry") or []
        if t.get("name") and t.get("covers")
    ]


# The two layers a hunt run needs. Same tools and the same dump as a compose one;
# what differs is that a hunt states beliefs to test where a compose states steps.
def resolve_hunt(
    workflow_id: str,
    model: Optional[str] = None,
    workflows: Optional["WorkflowsService"] = None,
    registry: Optional["MCPRegistry"] = None,
    provider: Optional[str] = None,
    effort: Optional[str] = None,
) -> Tuple[str, str]:
    from core.workflows.workflows_service import WorkflowsService

    definition = (workflows or WorkflowsService()).get_workflow(workflow_id)
    if definition is None:
        raise UnknownPlaybook(f"no such workflow: {workflow_id}")

    # Empty is the shipped case, not an error: what a hunt tests belongs to the caller.
    # A run with none from either source is refused in execute_workflow.
    hypotheses = _strings(definition.metadata.get("hypotheses"))

    playbook = {
        "name": definition.name,
        "description": definition.description,
        "use_case": definition.use_case,
        "trigger_examples": list(definition.trigger_examples),
        "objectives": _strings(definition.metadata.get("objectives")),
        "scope": dict(definition.metadata.get("scope") or {}),
        "directives": dict(definition.metadata.get("directives") or {}),
        "hypotheses": hypotheses,
        "data_domains": _strings(definition.metadata.get("data_domains")),
        "narrative": definition.body,
    }
    # The telemetry the site declares (#5291): nameable as a source, and told to
    # the lead with what each covers. Absent when it declares none, so the
    # playbook is then exactly the definition's.
    telemetry = _site_telemetry()
    if telemetry:
        playbook["data_domains"] = list(
            dict.fromkeys(playbook["data_domains"] + [t["name"] for t in telemetry])
        )
        playbook["site_telemetry"] = telemetry

    hunt_budgets, hunt_thresholds = _hunt_config_limits()
    config = {
        "model": model or DEFAULT_MODEL,
        **({"provider": provider} if provider else {}),
        **({"effort": effort} if effort else {}),
        **_instructions(),
        "budgets": hunt_budgets,
        "runtime": DEFAULT_RUNTIME,
        "tools": _bound_capabilities(list(HUNT_CAPABILITIES), _tool_catalogue(registry))
        + [_expand_tool()],
        # The hunt gates on its own checkpoint classes, which are a property of
        # what it is about to conclude rather than of a tool it happens to call.
        "approvals": [],
        "thresholds": hunt_thresholds,
        "hypothesis_loop": HUNT_HYPOTHESIS_LOOP,
    }

    # Checkpoint policies a definition declares. The agent merges these over its
    # DEFAULT_CHECKPOINTS, so an unset policy keeps the default; omit the key
    # entirely when the definition names none.
    checkpoints = _checkpoints(definition)
    if checkpoints:
        config["checkpoints"] = checkpoints

    return _dump(playbook), _dump(config)


# A root-cause trace is one investigator, not a hunt. The grant is the telemetry
# tool this deployment already bound; the v3 loop owns every other move.
def resolve_root_cause(
    workflow_id: str,
    model: Optional[str] = None,
    workflows: Optional["WorkflowsService"] = None,
    registry: Optional["MCPRegistry"] = None,
    provider: Optional[str] = None,
    effort: Optional[str] = None,
) -> Tuple[str, str]:
    from core.workflows.workflows_service import WorkflowsService

    definition = (workflows or WorkflowsService()).get_workflow(workflow_id)
    if definition is None:
        raise UnknownPlaybook(f"no such workflow: {workflow_id}")

    playbook = {
        "name": definition.name,
        "description": definition.description,
        "use_case": definition.use_case,
        "trigger_examples": list(definition.trigger_examples),
        "objectives": _strings(definition.metadata.get("objectives")),
        "scope": dict(definition.metadata.get("scope") or {}),
        "directives": dict(definition.metadata.get("directives") or {}),
        "narrative": definition.body,
    }

    config = {
        "model": model or DEFAULT_MODEL,
        **({"provider": provider} if provider else {}),
        **({"effort": effort} if effort else {}),
        **_instructions(),
        "budgets": {
            "max_calls": ROOT_CAUSE_MAX_CALLS,
            "max_cost_usd": HUNT_BUDGETS["max_cost_usd"],
            "max_wall_ms": HUNT_BUDGETS["max_wall_ms"],
        },
        "runtime": {**DEFAULT_RUNTIME, "max_turns": ROOT_CAUSE_MAX_TURNS},
        "tools": _bound_capabilities(
            list(ROOT_CAUSE_CAPABILITIES), _tool_catalogue(registry)
        ),
        "approvals": [],
        "thresholds": {},
    }

    checkpoints = _checkpoints(definition)
    if checkpoints:
        config["checkpoints"] = checkpoints

    return _dump(playbook), _dump(config)


# The policies the agent layer understands. Stated rather than imported, like
# RUN_KINDS: the class list is CHECKPOINT_CLASSES in the agent's checkpoints.ts,
# and only the two words are needed to tell a policy from a typo.
CHECKPOINT_POLICIES = ("ask", "auto")

# The four moments a hunt may stop and ask, and what each does unless a definition
# says otherwise. Mirrored from services/agent/plugins/hunt/checkpoints.ts, so the
# reader can show a class the engine really has.
CHECKPOINT_CLASSES = (
    "hypothesis_approval",
    "scope_extension",
    "verdict_review",
    "budget_anomaly",
)
DEFAULT_CHECKPOINTS = {
    "hypothesis_approval": "auto",
    "scope_extension": "auto",
    "verdict_review": "auto",
    "budget_anomaly": "auto",
}


# Refused here rather than shrugged at. A WORKFLOW.md's front matter is text nobody
# type-checks, and YAML reads `hypothesis_approval: yes` as the boolean True. The
# agent layer now reads anything that is not "auto" as "ask", so a typo can no
# longer switch an approval gate off -- but a run that parks when its author meant
# it not to is still an author being misread, and this is where they can be told.
# The class name is the agent's to know, so an unrecognised one passes through and
# is dropped there.
def _checkpoints(definition: "WorkflowDefinition") -> Dict[str, Any]:
    declared = definition.metadata.get("checkpoints") or {}
    if not isinstance(declared, dict):
        raise UnknownPlaybook(
            f"{definition.id}: checkpoints must be a mapping of "
            f"checkpoint class to {' or '.join(CHECKPOINT_POLICIES)}"
        )
    wrong = {
        name: policy
        for name, policy in declared.items()
        if policy not in CHECKPOINT_POLICIES
    }
    if wrong:
        stated = ", ".join(f"{name}: {policy!r}" for name, policy in wrong.items())
        raise UnknownPlaybook(
            f"{definition.id} declares checkpoint policies that are neither "
            f"{' nor '.join(CHECKPOINT_POLICIES)}: {stated}. Quote them if YAML is "
            "reading one as a boolean."
        )
    return dict(declared)


# Local, because the answer is the run's own ledger. Declared here so the lead's
# granted expand resolves to something rather than posting to a backend.
def _expand_tool() -> Dict[str, Any]:
    return {
        "id": "expand",
        "kind": "local",
        "description": "Return the raw payloads behind evidence ids from this run's own record.",
        "parameters": {
            "type": "object",
            "required": ["evidence_ids"],
            "properties": {
                "evidence_ids": {"type": "array", "items": {"type": "string"}}
            },
        },
    }


# Read at resolve time, which is run start: the agent layer journals the config
# with the spec, so the run, a resume and a replay all hold the same text.
def _instructions() -> Dict[str, str]:
    from core.llm.system_prompt import operator_instructions

    text = operator_instructions()
    return {"instructions": text} if text else {}


# Block style and no aliases: the agent layer parses this, and a YAML anchor would
# arrive as a shared reference nobody on that side asked for.
def _dump(document: Dict[str, Any]) -> str:
    return yaml.safe_dump(
        document, default_flow_style=False, sort_keys=False, allow_unicode=True
    )
