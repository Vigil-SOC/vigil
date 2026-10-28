"""Chat config assembly — the tool surface a chat turn is allowed to reach.

The load-bearing invariant here is the effect-class filter (#3304): chat has no
approval-resume path, so only a tool stored as read or read_unverified is
reachable. A reversible, irreversible or unclassified one is recommended, not
detonated.
"""

import pytest

from core.llm.chat_layers import _declare, integration_tools


def _mcp(name, description="something useful"):
    return {
        "name": name,
        "description": description,
        "input_schema": {"type": "object"},
    }


def _reachable(tools):
    return {t["name"] for t in integration_tools(tools)}


@pytest.mark.unit
def test_chat_reaches_only_read_classes(stored_tool_classes):
    stored_tool_classes.update(
        {
            "otx_check_ip": "read",
            "misp_misp_search_ioc": "read_unverified",
            "okta_reset_password": "reversible",
            "palo-alto_pan_block_ip": "irreversible",
        }
    )
    reachable = _reachable(
        [_mcp(name) for name in [*stored_tool_classes, "okta_get_user"]]
    )
    # okta_get_user has no row: unclassified, so out of reach like a write.
    assert reachable == {"otx_check_ip", "misp_misp_search_ioc"}


@pytest.mark.unit
def test_chat_reaches_vigils_own_tools_without_a_class(stored_tool_classes):
    # Vigil's own MCP tools arrive unprefixed; they are role checks, not classes.
    assert _reachable([_mcp("add_case_comment")]) == {"add_case_comment"}


@pytest.mark.unit
def test_an_unreadable_store_leaves_chat_no_outside_tool(monkeypatch):
    from core.response import ceiling

    def fail(_names):
        raise RuntimeError("database down")

    monkeypatch.setattr(ceiling, "_read_classes", fail)
    assert _reachable([_mcp("misp_misp_search_ioc")]) == set()


@pytest.mark.unit
def test_integration_tools_are_never_declared_one_by_one(stored_tool_classes):
    stored_tool_classes["virustotal_get_ip_report"] = "read_unverified"
    declared = {t["id"] for t in _declare(None, [_mcp("virustotal_get_ip_report")])}
    assert "virustotal_get_ip_report" not in declared
    assert {"find_integration_tools", "call_integration_tool"} <= declared


@pytest.mark.unit
def test_chat_config_keeps_approvals_empty_when_art_is_connected(
    stored_tool_classes,
):
    stored_tool_classes["atomic-red-team_atomic_red_team_execute"] = "irreversible"
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


@pytest.mark.unit
def test_chat_config_carries_context_window_only_when_known():
    import yaml

    from core.llm.chat_layers import chat_config

    assert (
        yaml.safe_load(chat_config("m", context_window=16384))["context_window"]
        == 16384
    )
    assert "context_window" not in yaml.safe_load(chat_config("m", context_window=0))
    assert "context_window" not in yaml.safe_load(chat_config("m"))
