"""Chat config assembly — the tool surface a chat turn is allowed to reach.

The load-bearing invariant here is the destructive-MCP filter: chat has no
approval-resume path, so a direct-action tool (host isolation, IP block) must
never be reachable by the assistant. It gets recommended, not detonated.
"""

import pytest

from core.llm.chat_layers import _declare, _is_destructive_mcp, integration_tools


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
        "mde_isolate",
        "crowdstrike_contain_host",
        "firewall_block_ip",
        "edr_kill_process",
        "defender_quarantine_file",
        "host_unisolate",
        "okta_revoke_session",
        "aws_terminate_instance",
        "atomic_red_team_execute",
        "atomic-red-team_atomic_red_team_execute",
    ],
)
def test_direct_action_tools_are_destructive(name):
    assert _is_destructive_mcp(name) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
        # A read-only lead verb wins even when a destructive noun follows.
        "crowdstrike_get_blocklist",
        "mde_get_isolation_status",
        "jira_list_contained_hosts",
        # Plainly read-only tools.
        "virustotal_get_ip_report",
        "shodan_search_host",
        "splunk_query",
        # ``execute`` is not a destructive verb: ART is dropped by id so this stays.
        "splunk-selfhosted_splunk_execute",
    ],
)
def test_read_only_tools_are_not_destructive(name):
    assert _is_destructive_mcp(name) is False


def _mcp(name, description="something useful"):
    return {
        "name": name,
        "description": description,
        "input_schema": {"type": "object"},
    }


def _reachable(tools):
    return {t["name"] for t in integration_tools(tools)}


@pytest.mark.unit
def test_chat_cannot_reach_destructive_mcp_but_reaches_the_rest():
    reachable = _reachable([_mcp("mde_isolate"), _mcp("virustotal_get_ip_report")])
    assert reachable == {"virustotal_get_ip_report"}


@pytest.mark.unit
def test_chat_cannot_reach_art_execute_but_reaches_splunk_execute():
    reachable = _reachable(
        [
            _mcp("atomic_red_team_execute"),
            _mcp("atomic-red-team_atomic_red_team_execute"),
            _mcp("splunk-selfhosted_splunk_execute"),
        ]
    )
    assert reachable == {"splunk-selfhosted_splunk_execute"}


@pytest.mark.unit
def test_integration_tools_are_never_declared_one_by_one():
    declared = {t["id"] for t in _declare(None, [_mcp("virustotal_get_ip_report")])}
    assert "virustotal_get_ip_report" not in declared
    assert {"find_integration_tools", "call_integration_tool"} <= declared


@pytest.mark.unit
def test_chat_config_keeps_approvals_empty_when_art_is_connected():
    import yaml

    from core.llm.chat_layers import chat_config

    config = yaml.safe_load(
        chat_config(
            "m",
            mcp_tools=[_mcp("atomic-red-team_atomic_red_team_execute")],
        )
    )
    assert config["approvals"] == []
    assert "atomic-red-team_atomic_red_team_execute" not in {
        tool["id"] for tool in config["tools"]
    }


@pytest.mark.unit
def test_chat_config_carries_effort_only_when_set():
    import yaml

    from core.llm.chat_layers import chat_config

    assert yaml.safe_load(chat_config("m", effort="high"))["effort"] == "high"
    assert "effort" not in yaml.safe_load(chat_config("m", effort=None))
