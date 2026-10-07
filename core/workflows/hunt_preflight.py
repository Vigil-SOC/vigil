"""Workflow preflight: what a run of this workflow is before it starts.

Who runs it, on which model, what it may do on its own, where it stops and where
it pauses, for every kind. The console asks before a run starts, so an operator
learns a hunt will run without a SIEM, and at what rate, while it still costs
nothing. This describes *executing* a workflow, not the definition, so it is
console wiring served at ``GET /api/workflows/{workflow_id}/preflight`` and kept
off the frozen catalog read. Every shape is read from the engine's own constants
(``playbook_resolver``, the definition's phases), never restated.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from core.llm.chat_layers import changes_for_tool
from core.workflows.workflows_service import (
    ADJUDICATE_RUN_KIND,
    COMPOSE_RUN_KIND,
    HUNT_RUN_KIND,
    ROOT_CAUSE_RUN_KIND,
    WorkflowDefinition,
    WorkflowsService,
    is_hunt_like,
)

logger = logging.getLogger(__name__)


# Read from the resolver, not restated, so it cannot drift from what runs are
# built on.
def hunt_defaults() -> Tuple[int, float, int]:
    from core.workflows.playbook_resolver import HUNT_BUDGETS, HUNT_THRESHOLDS

    return (
        HUNT_THRESHOLDS["max_iterations"],
        HUNT_BUDGETS["max_cost_usd"],
        HUNT_BUDGETS["max_wall_ms"],
    )


# Best effort: a registry that cannot be read reports nothing missing rather
# than blocking the modal.
def capabilities(registry: Any) -> Dict[str, Any]:
    from core.workflows.playbook_resolver import capability_report

    try:
        return capability_report(registry)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not read bound capabilities: %s", exc)
        return {"bound": [], "unbound": []}


# What the run will be charged at, and how confidently. An unpriced model is
# refused a few calls in, correctly but after the spend, so it is said here.
def pricing() -> Dict[str, Any]:
    from core.llm.cost.pricing_router import priced_as
    from core.llm.defaults import DEFAULT_MODEL
    from core.llm.providers.registry import get_registry

    try:
        provider, model = priced_as("bifrost", DEFAULT_MODEL)
        source = get_registry().get_pricing_source(model, provider)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not read the rate for the default model: %s", exc)
        return {"model": DEFAULT_MODEL, "source": "unknown"}
    return {"model": DEFAULT_MODEL, "source": source}


# What the lead of each engine holds besides its needs, as the arch yamls grant
# it (a ratchet reads them). Root cause's telemetry_search is a need, bound to a
# deployment tool; record and finish are the trace's own.
LEAD_TOOLS = {
    HUNT_RUN_KIND: ("expand",),
    ADJUDICATE_RUN_KIND: ("expand",),
    ROOT_CAUSE_RUN_KIND: ("telemetry_search", "record", "finish"),
}
LEAD_NAMES = {HUNT_RUN_KIND: "Hunt lead", ADJUDICATE_RUN_KIND: "Adjudication lead"}
# A single agent is a role, not one of the Agents tab's built-ins.
SINGLE_LEAD = "Lead analyst"
HANDOFF_IR = {
    "name": "HANDOFF_IR",
    "label": "Start incident response on a proven explanation",
    "changes": "on_its_own",
}


def _helper(phase: Dict[str, Any], tools: Optional[List[str]] = None) -> Dict[str, Any]:
    agent = phase.get("agent") or phase.get("agent_id") or ""
    return {
        "agent": agent,
        "name": phase.get("name") or agent,
        "tools": list(phase.get("tools") or []) if tools is None else tools,
        "approval_required": bool(phase.get("approval_required")),
    }


def _roles(definition: WorkflowDefinition) -> Tuple[Dict[str, Any], Optional[str]]:
    """``{lead, helpers, reviewer}`` for this kind, and a note when it is empty."""
    from core.workflows.playbook_resolver import INVESTIGATE_TOOLS, UnknownPlaybook

    kind = definition.run_kind
    if is_hunt_like(kind):
        return {
            "lead": {"name": LEAD_NAMES[kind], "tools": list(LEAD_TOOLS[kind])},
            "helpers": [_helper(p or {}) for p in definition.phases],
            "reviewer": {"name": "Critic", "tools": []},
        }, None
    if kind == ROOT_CAUSE_RUN_KIND:
        lead = {"name": SINGLE_LEAD, "tools": list(LEAD_TOOLS[kind])}
        return {"lead": lead, "helpers": [], "reviewer": None}, None
    if kind != COMPOSE_RUN_KIND:
        lead = {"name": SINGLE_LEAD, "tools": list(INVESTIGATE_TOOLS)}
        return {"lead": lead, "helpers": [], "reviewer": None}, None

    from core.workflows.playbook_resolver import _phases_of

    empty = {"lead": None, "helpers": [], "reviewer": None}
    try:
        phases = _phases_of(definition)
    except UnknownPlaybook as exc:
        return empty, f"Its phases cannot run as written: {exc}"
    if not phases:
        return empty, "This workflow declares no phases."
    return {**empty, "helpers": [_helper(p, p["tools"]) for p in phases]}, None


def _model() -> Tuple[Optional[str], Optional[str]]:
    """The ``investigation`` assignment every workflow run uses, and where it came from.

    The source is claimed only from the registry's own assignments: a model that
    resolves without one came from a default we did not check.
    """
    from core.agents.manager import _read_assignments
    from core.llm import target
    from core.llm.providers.registry import model_display_name

    resolved = target.resolve_component("investigation")
    if resolved is None:
        return None, None
    source = "assignment" if "investigation" in _read_assignments() else "default"
    return model_display_name(resolved[1]), source


def _skills(roles: Dict[str, Any]) -> Tuple[List[str], Optional[str]]:
    from core.skills.skill_library import READ_SKILL_TOOL, load_skills, skill_roots

    helpers = roles["helpers"]
    if any(READ_SKILL_TOOL in h["tools"] for h in helpers):
        library = [skill.name for skill in load_skills(skill_roots())]
        return library, None if library else "No skills are installed."
    if roles["lead"] is None:
        return [], "No phase can read a skill."
    if helpers:
        return [], "Hunt workers are bound to capabilities, not skills."
    return [], "The lead has no skill tool."


def _permissions(
    roles: Dict[str, Any], kind: str, report: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """One row per capability or tool the roles hold. ``asks_you`` only where a
    person approves first; read-only and unattended both act on their own."""
    names: List[str] = []
    for tools in [(roles["lead"] or {}).get("tools", [])] + [
        h["tools"] for h in roles["helpers"]
    ]:
        names += [t for t in tools if t not in names]
    rows = []
    for name in names:
        row: Dict[str, Any] = {
            "name": name,
            "changes": (
                "asks_you" if changes_for_tool(name) == "asks_first" else "on_its_own"
            ),
        }
        if name in report["unbound"]:
            row["bound"] = False
        elif name in report["bound"]:
            row["bound"] = True
        rows.append(row)
    if kind == HUNT_RUN_KIND:
        rows.append(dict(HANDOFF_IR))
    return rows


def _budgets(definition: WorkflowDefinition) -> Dict[str, Any]:
    from core.workflows import playbook_resolver as resolver

    kind = definition.run_kind
    if is_hunt_like(kind):
        max_iterations, max_cost_usd, max_wall_ms = hunt_defaults()
        return {
            "max_iterations": max_iterations,
            "max_cost_usd": max_cost_usd,
            "max_wall_ms": max_wall_ms,
        }
    if kind == ROOT_CAUSE_RUN_KIND:
        return {
            "max_turns": resolver.ROOT_CAUSE_MAX_TURNS,
            "max_cost_usd": resolver.HUNT_BUDGETS["max_cost_usd"],
            "max_wall_ms": resolver.HUNT_BUDGETS["max_wall_ms"],
        }
    # Only the count of phases sizes a budget, so a compose whose phases do not
    # resolve is still told its ceiling.
    return resolver._budgets(definition.phases)


def _checkpoints(
    definition: WorkflowDefinition,
) -> Tuple[Dict[str, str], Optional[str]]:
    from core.workflows.playbook_resolver import (
        CHECKPOINT_CLASSES,
        DEFAULT_CHECKPOINTS,
        UnknownPlaybook,
    )
    from core.workflows.playbook_resolver import _checkpoints as declared_by

    kind = definition.run_kind
    if not is_hunt_like(kind):
        if kind == COMPOSE_RUN_KIND:
            return {}, "It pauses only at phases marked approval_required."
        return {}, "This workflow never pauses to ask."
    note = None
    try:
        declared = declared_by(definition)
    except UnknownPlaybook as exc:
        declared, note = {}, f"Its declared policies are ignored: {exc}"
    merged = dict(DEFAULT_CHECKPOINTS)
    # An unknown class is dropped by the engine, so it is not shown.
    merged.update({c: p for c, p in declared.items() if c in CHECKPOINT_CLASSES})
    return merged, note


def preflight(
    service: WorkflowsService,
    registry: Any,
    workflow_id: str,
) -> Optional[Dict[str, Any]]:
    """What a run of this workflow is before it starts, ``None`` for no such id.

    Every kind answers ``roles``, ``model``, ``model_source``, ``skills``,
    ``permissions``, ``budgets`` and ``checkpoints``; an empty one carries its
    ``*_note``. A hunt-like kind also answers ``capabilities`` and ``pricing``,
    which the Run modal gates on.
    """
    definition = service.get_workflow(workflow_id)
    if definition is None:
        return None
    hunt_like = is_hunt_like(definition.run_kind)
    report = capabilities(registry) if hunt_like else {"bound": [], "unbound": []}
    roles, roles_note = _roles(definition)
    model, model_source = _model()
    skills, skills_note = _skills(roles)
    permissions = _permissions(roles, definition.run_kind, report)
    checkpoints, checkpoints_note = _checkpoints(definition)
    return {
        **({"capabilities": report, "pricing": pricing()} if hunt_like else {}),
        "roles": roles,
        "roles_note": roles_note,
        "model": model,
        "model_source": model_source,
        "skills": skills,
        "skills_note": skills_note,
        "permissions": permissions,
        "permissions_note": None if permissions else "It holds no tools.",
        "budgets": _budgets(definition),
        "checkpoints": checkpoints,
        "checkpoints_note": checkpoints_note,
    }
