"""Custom agents must resolve at playbook time and fail at create, not 404 (#870)."""

from __future__ import annotations

import pytest
import yaml

from core.agents.builtins import AgentProfile
from core.workflows.custom_workflow_service import _validate_agent_ids
from core.workflows.playbook_resolver import UnknownPlaybook, _prompt_for, resolve
from core.workflows.workflows_service import WorkflowDefinition

pytestmark = pytest.mark.unit


def _custom_profile(
    agent_id: str = "custom-foo", tools: list[str] | None = None
) -> AgentProfile:
    return AgentProfile(
        id=agent_id,
        name="Foo",
        description="",
        system_prompt="DISTINCTIVE CUSTOM PROMPT",
        icon="C",
        color="#888888",
        specialization="Custom",
        recommended_tools=list(tools or []),
    )


class _BoomManager:
    def __init__(self):
        raise AssertionError("built-in resolve must not construct AgentManager")


class _CustomManager:
    def __init__(self):
        self.agents = {"custom-foo": _custom_profile()}


def test_builtin_prompt_does_not_construct_agent_manager(monkeypatch):
    monkeypatch.setattr("core.agents.manager.AgentManager", _BoomManager)
    assert _prompt_for("triage")


def test_custom_prompt_comes_from_agent_manager(monkeypatch):
    monkeypatch.setattr("core.agents.manager.AgentManager", _CustomManager)
    assert _prompt_for("custom-foo") == "DISTINCTIVE CUSTOM PROMPT"


def test_unknown_custom_agent_raises(monkeypatch):
    monkeypatch.setattr("core.agents.manager.AgentManager", _CustomManager)
    with pytest.raises(UnknownPlaybook, match="custom-missing"):
        _prompt_for("custom-missing")


def test_unknown_non_custom_id_does_not_construct_agent_manager(monkeypatch):
    monkeypatch.setattr("core.agents.manager.AgentManager", _BoomManager)
    with pytest.raises(UnknownPlaybook, match="nope"):
        _prompt_for("nope")


def _definition(agent_id: str) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="wf-test",
        file_path=None,
        metadata={
            "name": "Test",
            "description": "d",
            "use_case": "",
            "trigger_examples": [],
            "phases": [
                {
                    "phase_id": "p1",
                    "agent_id": agent_id,
                    "name": "Phase 1",
                    "purpose": "do the thing",
                }
            ],
        },
        body="body",
        source="custom",
    )


class _Workflows:
    def __init__(self, agent_id: str):
        self._def = _definition(agent_id)

    def get_workflow(self, _id):
        return self._def


def test_resolve_builtin_phase_does_not_construct_agent_manager(monkeypatch):
    monkeypatch.setattr("core.agents.manager.AgentManager", _BoomManager)
    playbook, _ = resolve("wf-test", workflows=_Workflows("triage"))
    phase = yaml.safe_load(playbook)["phases"][0]
    assert phase["agent"] == "triage"
    assert phase["prompt"]


def test_resolve_carries_the_custom_agent_prompt(monkeypatch):
    monkeypatch.setattr("core.agents.manager.AgentManager", _CustomManager)

    playbook, config = resolve("wf-test", workflows=_Workflows("custom-foo"))
    phase = yaml.safe_load(playbook)["phases"][0]
    assert phase["agent"] == "custom-foo"
    assert phase["prompt"] == "DISTINCTIVE CUSTOM PROMPT"
    # The profile does not recommend read_skill, so the phase does not gain it.
    assert phase["tools"] == []
    assert "read_skill" not in [
        tool["id"] for tool in yaml.safe_load(config)["tools"]
    ]


def test_custom_phase_gains_read_skill_when_the_profile_recommends_it(monkeypatch):
    class _Granted:
        def __init__(self):
            self.agents = {
                "custom-foo": _custom_profile(
                    tools=[
                        "get_finding",
                        "recall_entity",
                        "list_cases",
                        "cf_lookup_ip_threat",
                        "read_skill",
                    ]
                )
            }

    monkeypatch.setattr("core.agents.manager.AgentManager", _Granted)
    definition = _definition("custom-foo")
    definition.metadata["phases"][0]["tools"] = ["lookup_indicators", "read_skill"]

    class _WorkflowsWithTools:
        def get_workflow(self, _id):
            return definition

    playbook, config = resolve("wf-test", workflows=_WorkflowsWithTools())
    phase = yaml.safe_load(playbook)["phases"][0]
    assert phase["tools"] == ["lookup_indicators", "read_skill"]
    assert phase["prompt"] == "DISTINCTIVE CUSTOM PROMPT"
    assert "read_skill" in [tool["id"] for tool in yaml.safe_load(config)["tools"]]


def test_validate_agent_ids_rejects_unknown(monkeypatch):
    monkeypatch.setattr("core.agents.manager.AgentManager", _CustomManager)
    _validate_agent_ids([{"agent_id": "custom-foo"}])
    with pytest.raises(ValueError, match="Unknown agent_id"):
        _validate_agent_ids([{"agent_id": "custom-nope"}])
