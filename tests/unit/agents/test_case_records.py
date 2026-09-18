"""case_records degrades to empty sections so an investigation can start (#867)."""

from __future__ import annotations

import pytest

from core.agents.tool_registry import execute_backend_tool
from core.llm.tool_schemas import ALL_TOOLS

pytestmark = pytest.mark.unit


def test_schema_names_case_records():
    assert any(tool.get("name") == "case_records" for tool in ALL_TOOLS)


class _NullWork:
    def __enter__(self):
        return object()

    def __exit__(self, *_exc):
        return False


@pytest.mark.asyncio
async def test_unknown_case_returns_empty_sections_not_an_error(monkeypatch):
    monkeypatch.setattr("core.cases.case_records_service.list_tasks", lambda case_id: [])
    monkeypatch.setattr(
        "core.cases.case_records_service.list_escalations",
        lambda session, case_id: [],
    )
    monkeypatch.setattr(
        "core.storage.unit_of_work.unit_of_work",
        lambda: _NullWork(),
    )

    result, handled = await execute_backend_tool(
        "case_records", {"case_id": "c-does-not-exist"}
    )

    assert handled is True
    assert result == {"tasks": [], "escalations": []}
    assert "error" not in result


@pytest.mark.asyncio
async def test_store_failure_is_empty_sections_not_refused(monkeypatch):
    monkeypatch.setattr("core.cases.case_records_service.list_tasks", lambda case_id: [])

    def _boom():
        raise RuntimeError("database is down")

    monkeypatch.setattr("core.storage.unit_of_work.unit_of_work", _boom)

    result, handled = await execute_backend_tool("case_records", {"case_id": "c-1"})

    assert handled is True
    assert result == {"tasks": [], "escalations": []}
