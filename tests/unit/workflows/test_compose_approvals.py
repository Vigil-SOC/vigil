"""Compose approvals: ART execute and direct-action tools are gated.

Shipped investigate and hunt definitions grant neither, so they stay ungated.
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


def test_a_direct_action_tool_is_gated_and_a_read_of_it_is_not():
    isolate = "test-edr_isolate_host"
    status = "test-edr_get_isolation_status"
    config = _config(
        [isolate, status, "get_finding", "cf_waf_block_ip"],
        isolate,
        status,
        "cf_waf_block_ip",
    )
    assert config["approvals"] == [isolate, "cf_waf_block_ip"]
    granted = [tool["id"] for tool in config["tools"]]
    assert status in granted and "get_finding" in granted


def test_incident_response_stays_ungated():
    _, config = resolve("incident-response")
    assert yaml.safe_load(config)["approvals"] == []


def test_hunt_stays_ungated():
    _, config = resolve_hunt("threat-hunt")
    assert yaml.safe_load(config)["approvals"] == []
