"""The agent list carries recommended_tools. The detail route already did."""

from core.agents.manager import AgentManager


def test_agent_list_includes_recommended_tools():
    rows = AgentManager().get_agent_list()
    triage = next(row for row in rows if row["id"] == "triage")
    assert "read_skill" in triage["recommended_tools"]
