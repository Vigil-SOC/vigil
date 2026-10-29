"""GET /agents/custom/_meta/tools lists every tool an agent may be given and
marks each read_only or asks_first."""

from core.integrations.mcp.registry import MCPRegistry
from core.integrations.mcp.surface import VIGIL_SERVER
from services.api.routers.custom_agents import list_available_tools


def registry(**servers):
    reg = MCPRegistry()
    for server, tools in servers.items():
        reg.register_server(server, tools=[{"name": n} for n in tools])
    return reg


def test_changes_marks_connected_and_built_in_tools(stored_tool_classes):
    stored_tool_classes.update(
        {"splunk_search": "read", "crowdstrike_isolate_host": "irreversible"}
    )
    body = list_available_tools(
        registry(
            splunk=["search", "get_events"],
            crowdstrike=["isolate_host"],
            okta=["get_user"],
            vigil=["create_approval_action"],
        )
    )
    assert "splunk_search" in body["tools"]
    changes = body["changes"]
    assert changes["splunk_search"] == "read_only"
    assert changes["crowdstrike_isolate_host"] == "asks_first"
    assert changes["create_approval_action"] == "asks_first"
    # A connected name nobody classified asks first.
    assert changes["okta_get_user"] == "asks_first"
    # Built-in tools are always there, connected or not.
    assert changes["read_skill"] == "read_only"
    # A name no one lists has no mark: the drawer shows it as not connected.
    assert "okta_revoke_session" not in changes


def test_catalog_lists_each_tool_under_the_server_that_serves_it(stored_tool_classes):
    reg = MCPRegistry()
    reg.register_server(
        "splunk",
        tools=[{"name": "search", "description": "Run an SPL search. Returns events."}],
    )
    # Vigil's own MCP tools are not prefixed; their names say nothing of the server.
    reg.register_server(
        VIGIL_SERVER, tools=[{"name": "get_case", "description": "Read a case."}]
    )
    body = list_available_tools(reg)
    rows = {r["name"]: r for r in body["catalog"]}
    assert rows["splunk_search"] == {
        "name": "splunk_search",
        "server": "splunk",
        "description": "Run an SPL search.",
    }
    assert rows["get_case"]["server"] == VIGIL_SERVER
    assert rows["read_skill"]["server"] == VIGIL_SERVER
    assert rows["read_skill"]["description"]
    # The catalog and the marks name the same tools; Vigil's come first.
    assert set(rows) == set(body["changes"])
    assert body["catalog"][-1]["server"] == "splunk"
