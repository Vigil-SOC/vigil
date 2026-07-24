"""Parity tests for ``services.tool_manager.execute_backend_tool`` (#393).

This is the single source of truth for built-in backend-tool dispatch after the
consolidation. These lock the two contract properties every caller (chat,
daemon, workflow) relies on:

  * a recognised backend tool returns ``(result, handled=True)``;
  * an unrecognised tool returns ``(None, handled=False)`` so the caller falls
    back to the MCP layer.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

pytestmark = pytest.mark.unit


def test_unknown_tool_returns_not_handled():
    from services.tool_manager import execute_backend_tool

    result, handled = asyncio.run(execute_backend_tool("splunk_search", {}))
    assert result is None
    assert handled is False


def test_list_findings_routes_to_data_service():
    from services import tool_manager

    findings = [
        {
            "finding_id": "f1",
            "severity": "high",
            "anomaly_score": 0.9,
            "data_source": "splunk",
            "timestamp": "2026-01-01T00:00:00Z",
            "status": "open",
            "description": "Test finding",
        }
    ]
    with patch("services.database_data_service.DatabaseDataService") as mock_ds_cls:
        mock_ds = mock_ds_cls.return_value
        mock_ds.count_findings.return_value = 1
        mock_ds.get_findings.return_value = findings
        result, handled = asyncio.run(
            tool_manager.execute_backend_tool(
                "list_findings", {"limit": 10, "offset": 0}
            )
        )

    assert handled is True
    assert result["total"] == 1
    assert result["findings"][0]["finding_id"] == "f1"
