# The preflight is about executing a run, so it has its own console route and the
# frozen catalog read stays the definition. Every kind answers the same keys.

from __future__ import annotations

import pytest

from core.llm.chat_layers import changes_for_tool, changes_for_tools
from core.workflows import catalog, hunt_preflight
from core.workflows.workflows_service import WorkflowDefinition, WorkflowsService

pytestmark = pytest.mark.unit

HUNT_ONLY_KEYS = {"capabilities", "pricing"}
EVERY_KIND_KEYS = {
    "roles",
    "roles_note",
    "model",
    "model_source",
    "skills",
    "skills_note",
    "permissions",
    "permissions_note",
    "budgets",
    "checkpoints",
    "checkpoints_note",
}


class _Service:
    def __init__(self, *definitions):
        self._by_id = {d.id: d for d in definitions}

    def get_workflow(self, workflow_id):
        return self._by_id.get(workflow_id)


def _compose(*phases, **extra):
    return WorkflowDefinition(
        workflow_id="mine",
        file_path=None,
        metadata={"run_kind": "compose", "phases": list(phases), **extra},
        body="",
        source="custom",
    )


def _ask(definition, registry=None):
    return hunt_preflight.preflight(_Service(definition), registry, definition.id)


def _file(workflow_id):
    return hunt_preflight.preflight(WorkflowsService(), None, workflow_id)


@pytest.fixture(autouse=True)
def _model_unset(monkeypatch):
    # No DB here: nothing resolves, which is itself one of the cases.
    from core.llm import target

    monkeypatch.setattr(target, "resolve_component", lambda component: None)


def _assignments(monkeypatch, assigned):
    from core.agents import manager
    from core.llm import target

    monkeypatch.setattr(
        target, "resolve_component", lambda component: ("anthropic", "claude-x")
    )
    monkeypatch.setattr(manager, "_read_assignments", lambda: assigned)


@pytest.mark.parametrize(
    "workflow_id",
    ["threat-hunt", "shadow-adjudication", "incident-response", "root-cause-analysis"],
)
def test_every_kind_answers_every_key_and_an_empty_one_says_why(workflow_id):
    report = _file(workflow_id)

    assert EVERY_KIND_KEYS <= set(report)
    for key in ("roles", "skills", "permissions", "checkpoints"):
        note = report[f"{key}_note"] if key != "roles" else report["roles_note"]
        value = report[key]
        empty = not value or (key == "roles" and not value["lead"])
        assert not empty or note, key
    # Only a hunt-like kind has capabilities to miss and a rate to warn of.
    assert (HUNT_ONLY_KEYS <= set(report)) == (
        workflow_id in {"threat-hunt", "shadow-adjudication"}
    )


def test_a_hunt_names_lead_helpers_reviewer_and_its_ceilings():
    report = _file("threat-hunt")

    assert report["roles"]["lead"] == {"name": "Hunt lead", "tools": ["expand"]}
    assert [h["agent"] for h in report["roles"]["helpers"]] == [
        "threat_hunter",
        "network_analyst",
        "threat_intel",
    ]
    assert report["roles"]["reviewer"] == {"name": "Critic", "tools": []}
    assert set(report["budgets"]) == {"max_iterations", "max_cost_usd", "max_wall_ms"}
    assert report["checkpoints"] == {
        "hypothesis_approval": "auto",
        "scope_extension": "auto",
        "verdict_review": "auto",
        "budget_anomaly": "auto",
    }
    assert report["skills"] == [] and report["skills_note"]
    assert set(report["capabilities"]) >= {"bound", "unbound"}


def test_only_a_hunt_can_hand_off_to_incident_response():
    handoff = [r["name"] for r in _file("threat-hunt")["permissions"]]
    adjudicate = [r["name"] for r in _file("shadow-adjudication")["permissions"]]

    assert "HANDOFF_IR" in handoff
    assert "HANDOFF_IR" not in adjudicate


def test_an_unbound_capability_is_marked(monkeypatch):
    monkeypatch.setattr(
        hunt_preflight,
        "capabilities",
        lambda registry: {
            "bound": ["findings_search"],
            "unbound": ["telemetry_search", "indicator_lookup", "entity_recall"],
        },
    )
    rows = {r["name"]: r for r in _file("threat-hunt")["permissions"]}

    assert rows["telemetry_search"]["bound"] is False
    assert rows["findings_search"]["bound"] is True
    assert "bound" not in rows["expand"]


def test_a_single_agent_is_a_lead_analyst_with_its_tools_and_no_checkpoints():
    report = _file("incident-response")

    assert report["roles"]["lead"] == {
        "name": "Lead analyst",
        "tools": ["case_records", "get_finding"],
    }
    assert report["roles"]["helpers"] == [] and report["roles"]["reviewer"] is None
    assert {r["name"] for r in report["permissions"]} == {"case_records", "get_finding"}
    assert report["checkpoints"] == {} and report["checkpoints_note"]
    assert set(report["budgets"]) == {"max_calls", "max_cost_usd", "max_wall_ms"}


def test_root_cause_is_given_its_turns_and_the_hunt_cost_and_wall():
    from core.workflows.playbook_resolver import HUNT_BUDGETS, ROOT_CAUSE_MAX_TURNS

    report = _file("root-cause-analysis")

    assert report["budgets"] == {
        "max_turns": ROOT_CAUSE_MAX_TURNS,
        "max_cost_usd": HUNT_BUDGETS["max_cost_usd"],
        "max_wall_ms": HUNT_BUDGETS["max_wall_ms"],
    }
    assert "telemetry_search" in report["roles"]["lead"]["tools"]


def test_a_compose_lists_its_phases_and_its_skills_when_one_can_read_them():
    mine = _compose(
        {"id": "a", "agent": "triage", "name": "Triage it", "tools": ["create_case"]},
        {"id": "b", "agent": "investigator", "name": "Dig", "approval_required": True},
    )
    report = _ask(mine)

    helpers = report["roles"]["helpers"]
    assert [(h["agent"], h["name"], h["approval_required"]) for h in helpers] == [
        ("triage", "Triage it", False),
        ("investigator", "Dig", True),
    ]
    # triage recommends read_skill, so the phase gets it whether or not it listed it.
    assert "read_skill" in helpers[0]["tools"]
    assert report["skills"] and report["skills_note"] is None
    assert report["roles"]["lead"] is None
    assert report["checkpoints"] == {} and report["checkpoints_note"]
    assert report["budgets"]["max_calls"] == 2 * 10
    assert HUNT_ONLY_KEYS.isdisjoint(report)


def test_a_compose_with_no_skill_tool_has_no_skills_and_says_so(monkeypatch):
    from types import SimpleNamespace

    from core.workflows import playbook_resolver

    # Every built-in agent recommends read_skill, so a bare one is a stand-in.
    bare = SimpleNamespace(recommended_tools=["get_finding"], system_prompt="")
    monkeypatch.setattr(playbook_resolver, "_profile_for", lambda agent_id: bare)
    report = _ask(_compose({"agent": "bare", "name": "Dig"}))

    assert "read_skill" not in report["roles"]["helpers"][0]["tools"]
    assert report["skills"] == [] and report["skills_note"]


def test_a_compose_that_cannot_resolve_answers_with_a_note_not_an_error():
    broken = _compose({"agent": "no_such_agent", "name": "Ghost"})
    empty = _compose()

    for definition in (broken, empty):
        report = _ask(definition)
        assert report["roles"]["helpers"] == [] and report["roles_note"]
        assert report["permissions"] == [] and report["permissions_note"]
        assert EVERY_KIND_KEYS <= set(report)


def test_art_execute_in_a_compose_asks_you():
    from core.integrations.atomic_red_team.descriptor import EXECUTE_IDS

    execute = sorted(EXECUTE_IDS)[0]
    report = _ask(
        _compose({"agent": "mitre_analyst", "name": "Run", "tools": [execute]})
    )

    rows = {r["name"]: r["changes"] for r in report["permissions"]}
    assert rows[execute] == "asks_you"


def test_the_two_word_form_narrows_the_three_the_classifier_gives():
    report = _ask(
        _compose(
            {
                "agent": "auto_responder",
                "name": "Act",
                "tools": ["create_approval_action", "get_finding"],
            }
        )
    )
    rows = {r["name"]: r["changes"] for r in report["permissions"]}

    assert rows["create_approval_action"] == "asks_you"
    assert rows["get_finding"] == "on_its_own"


def test_a_declared_checkpoint_policy_is_merged_over_the_defaults():
    hunt = WorkflowDefinition(
        workflow_id="h",
        file_path=None,
        metadata={
            "run_kind": "hunt",
            "checkpoints": {"verdict_review": "ask", "no_such_class": "ask"},
        },
        body="",
    )
    checkpoints = _ask(hunt)["checkpoints"]

    assert checkpoints["verdict_review"] == "ask"
    assert checkpoints["hypothesis_approval"] == "auto"
    assert "no_such_class" not in checkpoints


def test_a_malformed_checkpoint_policy_falls_back_to_the_defaults_with_a_note():
    hunt = WorkflowDefinition(
        workflow_id="h",
        file_path=None,
        metadata={"run_kind": "hunt", "checkpoints": {"verdict_review": True}},
        body="",
    )
    report = _ask(hunt)

    assert set(report["checkpoints"].values()) == {"auto"}
    assert report["checkpoints_note"]


def test_the_model_source_is_assignment_only_when_one_is_stored(monkeypatch):
    _assignments(monkeypatch, {"investigation": "claude-x"})
    assert _file("threat-hunt")["model_source"] == "assignment"

    _assignments(monkeypatch, {"chat_default": "claude-x"})
    report = _file("threat-hunt")
    assert (report["model"], report["model_source"]) == ("claude-x", "default")


def test_no_model_resolving_is_null_not_a_guess():
    report = _file("threat-hunt")

    assert report["model"] is None and report["model_source"] is None


def test_the_one_tool_classifier_agrees_with_the_list_form():
    tools = ["get_finding", "create_approval_action", "block_ip", "create_case"]

    for tool in tools:
        assert changes_for_tools([tool]) == changes_for_tool(tool)
    assert changes_for_tool("get_finding") == "read_only"
    assert changes_for_tool("create_approval_action") == "asks_first"
    assert changes_for_tool("block_ip") == "on_its_own"
    # The list is as hands-on as its most hands-on tool, in any order.
    assert changes_for_tools(tools) == changes_for_tools(tools[::-1]) == "on_its_own"
    assert changes_for_tools([]) == "read_only"


def test_the_catalog_read_of_a_hunt_carries_no_preflight():
    detail = catalog.detail(WorkflowsService(), "threat-hunt")

    assert detail["hunt_like"] is True
    # The definition read already carries its declared checkpoints; the rest is ours.
    ours = (HUNT_ONLY_KEYS | EVERY_KIND_KEYS) - {"checkpoints"}
    assert not ours & set(detail)


def test_an_unknown_id_is_none():
    assert (
        hunt_preflight.preflight(WorkflowsService(), None, "no-such-workflow") is None
    )
