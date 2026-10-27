"""GET /agents/custom/_meta/tools marks each tool read_only, asks_first or on_its_own."""

from types import SimpleNamespace

from services.api.routers.custom_agents import list_available_tools


def registry(*names):
    return SimpleNamespace(get_tool_names=lambda: list(names))


def test_changes_marks_connected_and_built_in_tools():
    body = list_available_tools(
        registry(
            "splunk_search",
            "crowdstrike_isolate_host",
            "splunk_get_events",
            "create_approval_action",
        )
    )
    assert "splunk_search" in body["tools"]
    changes = body["changes"]
    assert changes["splunk_search"] == "read_only"
    assert changes["crowdstrike_isolate_host"] == "asks_first"
    assert changes["create_approval_action"] == "asks_first"
    # Built-in tools are always there, connected or not.
    assert changes["read_skill"] == "read_only"
    # A name no one lists has no mark: the drawer shows it as not connected.
    assert "okta_revoke_session" not in changes


def test_catalog_says_where_each_tool_comes_from_and_what_it_does():
    reg = SimpleNamespace(
        get_tool_names=lambda: ["splunk_search"],
        get_all_tools=lambda: [
            {
                "name": "splunk_search",
                "description": "[splunk] Run an SPL search. Returns events.",
            }
        ],
        tool_servers=lambda: {"splunk_search": "splunk"},
    )
    rows = {r["name"]: r for r in list_available_tools(reg)["catalog"]}
    assert rows["splunk_search"]["server"] == "splunk"
    assert rows["splunk_search"]["description"] == "Run an SPL search."
    # Vigil's own tools are listed under "vigil", grouped by what they work on.
    assert rows["get_finding"]["server"] == "vigil"
    assert rows["get_finding"]["group"] == "Findings"
    assert rows["list_cases"]["group"] == "Cases"
    assert all(r["description"] for r in rows.values() if r["server"] == "vigil")
