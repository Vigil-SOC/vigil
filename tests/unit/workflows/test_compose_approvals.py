"""Compose approvals: ART execute is gated; incident-response is not (#837).

In Assist, a granted tool that changes something on its own is gated too.
"""

from __future__ import annotations

from typing import List

import pytest
import yaml

from core.integrations.atomic_red_team.descriptor import (
    EXECUTE_TOOL,
    MCP_EXECUTE_TOOL,
)
from core.workflows.playbook_resolver import resolve, resolve_hunt
from core.workflows.workflows_service import WorkflowDefinition

pytestmark = pytest.mark.unit


class _Registry:
    def __init__(self, *names: str):
        self._names = names

    def get_all_tools(self):
        return [
            {
                "name": name,
                "description": f"stub {name}",
                "input_schema": {"type": "object"},
            }
            for name in self._names
        ]


class _Workflows:
    def __init__(self, tools: List[str]):
        self._def = WorkflowDefinition(
            workflow_id="art-grant",
            file_path=None,
            metadata={
                "name": "ART grant",
                "description": "d",
                "use_case": "",
                "trigger_examples": [],
                "phases": [
                    {
                        "id": "assess",
                        "agent": "mitre_analyst",
                        "name": "Assess",
                        "tools": tools,
                        "instructions": "assess coverage then execute",
                    }
                ],
            },
            body="",
            source="custom",
        )

    def get_workflow(self, _id):
        return self._def


def _config(tools: List[str], *catalogue: str):
    _, config = resolve(
        "art-grant",
        workflows=_Workflows(tools),
        registry=_Registry(*catalogue),
    )
    return yaml.safe_load(config)


@pytest.mark.parametrize("execute_id", [EXECUTE_TOOL, MCP_EXECUTE_TOOL])
def test_compose_phase_that_grants_execute_puts_it_in_approvals(execute_id):
    config = _config([execute_id, "analyze_coverage"], execute_id)
    ids = [tool["id"] for tool in config["tools"]]
    assert execute_id in ids
    assert "analyze_coverage" in ids
    assert config["approvals"] == [execute_id]


def test_incident_response_stays_ungated():
    _, config = resolve("incident-response")
    assert yaml.safe_load(config)["approvals"] == []


def test_hunt_stays_ungated():
    _, config = resolve_hunt("threat-hunt")
    assert yaml.safe_load(config)["approvals"] == []


WRITE = "cloudflare_cf_waf_block_ip"
LOOKUP = "cloudflare_list_blocked_ips"


@pytest.mark.parametrize(("assist", "gated"), [(True, [WRITE]), (False, [])])
def test_assist_gates_a_granted_write_tool_and_act_does_not(monkeypatch, assist, gated):
    monkeypatch.setattr("core.response.approval_service.assist_on", lambda: assist)
    config = _config([WRITE, LOOKUP], WRITE, LOOKUP)
    assert {WRITE, LOOKUP} <= {tool["id"] for tool in config["tools"]}
    assert config["approvals"] == gated


def test_assist_gates_execute_and_a_write_tool_together(monkeypatch):
    monkeypatch.setattr("core.response.approval_service.assist_on", lambda: True)
    config = _config([EXECUTE_TOOL, WRITE], EXECUTE_TOOL, WRITE)
    assert config["approvals"] == [EXECUTE_TOOL, WRITE]


def test_assist_is_not_read_when_no_write_tool_is_granted(monkeypatch):
    def unread():
        raise AssertionError("assist_on read for a grant with no write tool")

    monkeypatch.setattr("core.response.approval_service.assist_on", unread)
    assert _config([LOOKUP], LOOKUP)["approvals"] == []
    _, config = resolve("incident-response")
    assert yaml.safe_load(config)["approvals"] == []
