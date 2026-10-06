"""The agent list carries recommended_tools. The detail route already did."""

from core.agents.manager import AgentManager


def test_agent_list_includes_recommended_tools():
    rows = AgentManager().get_agent_list()
    triage = next(row for row in rows if row["id"] == "triage")
    assert "read_skill" in triage["recommended_tools"]


def test_builtin_names_descriptions_and_icons_match_the_board():
    rows = {row["id"]: row for row in AgentManager().get_agent_list()}
    assert rows["triage"]["name"] == "Triage agent"
    assert (
        rows["triage"]["description"]
        == "Scores incoming alerts and groups them into cases"
    )
    assert rows["threat_intel"]["name"] == "Threat intel agent"
    assert rows["threat_intel"]["icon"] == "TI"
    assert (
        rows["auto_responder"]["description"]
        == "Blocks and revokes within approved limits"
    )


def test_blank_icon_stays_blank():
    from core.agents.manager import SOCAgentLibrary

    profile = SOCAgentLibrary.build_profile(
        {"id": "custom-x", "name": "X", "icon": None}
    )
    assert profile.icon == ""
