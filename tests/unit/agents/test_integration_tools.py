"""Ask finds and calls integration tools on demand instead of declaring them (#1959)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.agents import internal_auth, tools_router
from core.agents.integration_tools import find_integration_tools
from core.integrations.mcp import client as mcp_client
from core.integrations.mcp import in_process
from core.integrations.mcp import registry as mcp_registry
from core.integrations.mcp.registry import MCPRegistry
from core.llm.chat_layers import _is_destructive_mcp, chat_config, integrations_line

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "mcp"
SD = "security-detections"
SPLUNK = "splunk-selfhosted"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def registry(monkeypatch):
    """A default install: Vigil's own server, security-detections and Splunk."""
    # The registry is the test's; nothing is read from a running client.
    monkeypatch.setattr(mcp_registry, "refresh_from_client", lambda _r: 0)
    reg = MCPRegistry()
    reg.register_server("vigil", {}, in_process.list_tools())
    reg.register_server(SD, {}, _fixture("security_detections_tools.json"))
    reg.register_server(SPLUNK, {}, _fixture("splunk_selfhosted_tools.json"))
    return reg


def _declared(reg):
    return yaml.safe_load(chat_config("m", None, reg.get_all_tools()))["tools"]


def test_a_default_install_declares_a_small_fixed_catalogue(registry):
    tools = _declared(registry)
    ids = {t["id"] for t in tools}
    assert len(tools) <= 40
    assert len(json.dumps(tools)) < 30_000
    assert {"find_integration_tools", "call_integration_tool"} <= ids
    assert not [i for i in ids if i.startswith((f"{SD}_", f"{SPLUNK}_"))]


def test_nothing_connected_declares_neither_tool_nor_line():
    ids = {t["id"] for t in yaml.safe_load(chat_config("m", None, None))["tools"]}
    assert not ids & {"find_integration_tools", "call_integration_tool"}
    assert integrations_line(None, {}) == ""


def test_the_prompt_line_names_the_connected_servers(registry):
    line = integrations_line(registry.get_all_tools(), registry.tool_servers())
    assert f"{SD}, {SPLUNK}, vigil" in line
    assert "find_integration_tools" in line


def test_find_returns_matching_tools_with_schemas_and_nothing_destructive(registry):
    matches = find_integration_tools(registry, "sigma rules for T1059")
    names = [m["name"] for m in matches]
    assert 0 < len(matches) <= 8
    assert f"{SD}_list_by_mitre" in names
    assert all(m["input_schema"].get("type") == "object" for m in matches)
    for query in ("sigma rules for T1059", "delete entity", "delete template"):
        found = find_integration_tools(registry, query)
        assert not [m["name"] for m in found if _is_destructive_mcp(m["name"])]


def test_find_can_be_narrowed_to_one_server(registry):
    matches = find_integration_tools(registry, "search", server=SPLUNK)
    assert matches and {m["server"] for m in matches} == {SPLUNK}


def test_connecting_a_server_changes_what_is_found_not_what_is_declared(registry):
    before = _declared(registry)
    assert not find_integration_tools(registry, "falcon")
    registry.register_server(
        "crowdstrike",
        {},
        [
            {
                "name": f"get_detections_{i}",
                "description": f"Falcon host detections {i}",
            }
            for i in range(40)
        ],
    )
    assert find_integration_tools(registry, "falcon")
    assert _declared(registry) == before


# --- call_integration_tool through /internal/tools/invoke ---------------------


class _Client:
    def __init__(self):
        self.calls = []

    async def call_tool(self, server, tool, args, timeout=None):
        self.calls.append((server, tool, args))
        return {"content": [{"type": "text", "text": json.dumps([{"id": "r1"}])}]}


@pytest.fixture
def invoke(monkeypatch, registry):
    monkeypatch.setattr(internal_auth, "get_secret", lambda name: "shhh")
    fake = _Client()
    monkeypatch.setattr(mcp_client, "process_mcp_client", lambda: fake)
    app = FastAPI()
    app.state.mcp_registry = registry
    app.include_router(tools_router.router, prefix=tools_router.ROUTER_META.prefix)
    http = TestClient(app)

    def call(tool, args):
        body = {
            "tool": tool,
            "args": args,
            "bounds": {"max_rows": 2, "timeout_ms": 5000},
        }
        return http.post(
            "/internal/tools/invoke",
            json=body,
            headers={"Authorization": "Bearer shhh"},
        ).json()

    call.client = fake
    return call


def test_call_runs_a_connected_read_tool_under_the_same_bounds(invoke):
    body = invoke(
        "call_integration_tool",
        {"name": f"{SD}_search", "arguments": {"query": "powershell"}},
    )
    assert body["ok"] is True
    assert body["rows"] == [{"id": "r1"}]
    assert body["sourceSystem"] == SD
    # The row cap reached the integration's own arguments.
    assert invoke.client.calls == [(SD, "search", {"query": "powershell", "limit": 2})]


@pytest.mark.parametrize(
    "name",
    [
        "nobody_has_this_tool",
        f"{SD}_delete_entity",
        "atomic-red-team_atomic_red_team_execute",
        "list_findings",  # a built-in: the agent's own grant decides it
    ],
)
def test_call_refuses_what_chat_may_not_reach(invoke, registry, name):
    registry.register_server(
        "atomic-red-team", {}, [{"name": "atomic_red_team_execute", "description": "x"}]
    )
    body = invoke("call_integration_tool", {"name": name, "arguments": {}})
    assert body == {
        "ok": False,
        "failure": {"kind": "refused", "detail": f"{name} is not granted to chat"},
    }
    assert invoke.client.calls == []


def test_find_runs_through_the_invoke_route(invoke):
    body = invoke(
        "find_integration_tools", {"query": "splunk search", "server": SPLUNK}
    )
    assert body["ok"] is True and body["rowCount"] == 2
    assert all(row["server"] == SPLUNK for row in body["rows"])
