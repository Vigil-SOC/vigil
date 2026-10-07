"""The backend tool manifest is usable (offline; no API key required)."""

from core.llm.tool_schemas import ALL_TOOLS


def test_tool_availability():
    assert len(ALL_TOOLS) > 0
    for tool in ALL_TOOLS:
        assert tool.get("name")
        assert tool.get("description")
