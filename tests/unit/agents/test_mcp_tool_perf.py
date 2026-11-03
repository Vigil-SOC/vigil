"""Unit tests for MCP and built-in agent tool performance fixes (#3673)."""

import json
import logging
from unittest.mock import MagicMock, patch

from core.agents import tool_registry
from core.cases import case_journal_service
from core.integrations.mcp import in_process
from core.integrations.mcp.registry import MCPRegistry
from tools.mcp import vigil as vigil_mcp


def test_tool_registry_data_reuses_singleton() -> None:
    """tool_registry._data() should reuse a single DatabaseDataService instance."""
    tool_registry._data_service = None
    with patch("core.storage.database_data_service.DatabaseDataService") as mock_cls:
        first = tool_registry._data()
        second = tool_registry._data()

        assert first is second
        assert mock_cls.call_count == 1
    tool_registry._data_service = None


def test_list_cases_pushes_filters_and_limit_to_get_cases() -> None:
    """tool_registry.list_cases should pass status, priority, and limit to get_cases."""
    mock_data = MagicMock()
    mock_data.get_cases.return_value = [
        {"case_id": "case-1", "status": "open", "priority": "high"}
    ]
    with patch("core.agents.tool_registry._data", return_value=mock_data):
        res = tool_registry.list_cases(status="open", priority="high", limit=25)

    assert len(res) == 1
    mock_data.get_cases.assert_called_once_with(
        limit=25, status="open", priority="high"
    )


def test_bulk_add_findings_to_case_batches_into_single_read_and_write() -> None:
    """bulk_add_findings_to_case should read and update the case only once."""
    mock_service = MagicMock()
    mock_service.get_case.return_value = {
        "case_id": "case-1",
        "finding_ids": ["f-existing"],
    }
    mock_service.update_case.return_value = True

    with (
        patch("tools.mcp.vigil.permission_refusal", return_value=None),
        patch("tools.mcp.vigil.get_data_service", return_value=mock_service),
    ):
        raw = vigil_mcp.bulk_add_findings_to_case(
            "case-1", ["f-1", "f-existing", "f-2", "f-1"]
        )

    payload = json.loads(raw)
    assert payload["success"] is True
    assert payload["added_findings"] == ["f-1", "f-2"]
    assert payload["failed_findings"] == ["f-existing", "f-1"]
    assert mock_service.get_case.call_count == 1
    mock_service.update_case.assert_called_once_with(
        "case-1", finding_ids=["f-existing", "f-1", "f-2"]
    )


def test_link_findings_returns_none_for_missing_case() -> None:
    """case_journal_service.link_findings should return None when the case is absent."""
    mock_service = MagicMock()
    mock_service.get_case.return_value = None
    assert case_journal_service.link_findings(mock_service, "missing", ["f-1"]) is None
    mock_service.update_case.assert_not_called()


def test_mcp_registry_suppresses_duplicate_info_log_and_caches_in_process_tools(
    caplog,
) -> None:
    """register_server should log INFO only once for unchanged tools; list_tools caches."""
    registry = MCPRegistry()
    tools = [{"name": "t1", "description": "d", "input_schema": {}}]

    with caplog.at_level(logging.INFO, logger="core.integrations.mcp.registry"):
        registry.register_server("vigil", tools)
        registry.register_server("vigil", tools)

    info_records = [
        r for r in caplog.records if "Registered MCP server: vigil" in r.message
    ]
    assert len(info_records) == 1

    in_process._cached_tools = None
    first = in_process.list_tools()
    second = in_process.list_tools()
    assert first is second
    assert len(first) > 0
