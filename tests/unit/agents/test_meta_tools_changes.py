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
    assert changes["crowdstrike_isolate_host"] == "on_its_own"
    assert changes["create_approval_action"] == "asks_first"
    # Built-in tools are always there, connected or not.
    assert changes["read_skill"] == "read_only"
    # A name no one lists has no mark: the drawer shows it as not connected.
    assert "okta_revoke_session" not in changes
