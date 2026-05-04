"""Unit tests for tools/vstrike.py (MCP server)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tools.vstrike as vstrike_module  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_service_cache():
    """Ensure each test starts with a fresh service lookup."""
    yield


@pytest.fixture
def mock_service():
    svc = MagicMock()
    svc.get_asset_topology.return_value = {"asset_id": "a1", "segment": "dmz"}
    svc.list_adjacent.return_value = [{"asset_id": "a2", "hop_distance": 1}]
    svc.get_blast_radius.return_value = {"count": 5, "sample": ["a3"]}
    svc.find_findings_by_segment.return_value = [{"finding_id": "f1"}]
    svc.node_search.return_value = [{"node_id": "n1", "node_name": "Router"}]
    svc.node_drift_get.return_value = [{"timestamp": "t1", "source": "cve"}]
    svc.storyline_list.return_value = [{"storyline_id": "s1", "name": "Exfil"}]
    svc.storyline_events_get.return_value = [{"event_id": "e1", "timestamp": "t1"}]
    svc.legend_run_list.return_value = [{"legend_run_id": "lr1", "name": "CVE"}]
    svc.legend_run_results_get.return_value = {"critical": 3}
    return svc


# ---------------------------------------------------------------------------
# Tool listing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_list_tools_includes_all_tools():
    tools = await vstrike_module.handle_list_tools()
    names = {t.name for t in tools}
    expected = {
        "vstrike_get_asset_topology",
        "vstrike_list_adjacent_assets",
        "vstrike_get_blast_radius",
        "vstrike_get_segment_findings",
        "vstrike_node_search",
        "vstrike_node_drift_get",
        "vstrike_storyline_list",
        "vstrike_storyline_events_get",
        "vstrike_legend_run_list",
        "vstrike_legend_run_results_get",
    }
    assert expected.issubset(names)


# ---------------------------------------------------------------------------
# Existing tools (regression check)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_tool_get_asset_topology(mock_service):
    with patch.object(vstrike_module, "_get_service", return_value=mock_service):
        result = await vstrike_module.handle_call_tool(
            "vstrike_get_asset_topology", {"asset_id": "a1"}
        )
    data = json.loads(result[0].text)
    assert data["asset_id"] == "a1"
    assert data["topology"]["segment"] == "dmz"


# ---------------------------------------------------------------------------
# New data-plane tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_tool_node_search(mock_service):
    with patch.object(vstrike_module, "_get_service", return_value=mock_service):
        result = await vstrike_module.handle_call_tool(
            "vstrike_node_search", {"query": "router", "network_id": "net-1", "limit": 10}
        )
    data = json.loads(result[0].text)
    assert data["query"] == "router"
    assert data["results"] == [{"node_id": "n1", "node_name": "Router"}]
    mock_service.node_search.assert_called_once_with("router", network_id="net-1", limit=10)


@pytest.mark.asyncio
async def test_call_tool_node_search_requires_query(mock_service):
    with patch.object(vstrike_module, "_get_service", return_value=mock_service):
        result = await vstrike_module.handle_call_tool(
            "vstrike_node_search", {"network_id": "net-1"}
        )
    data = json.loads(result[0].text)
    assert "error" in data


@pytest.mark.asyncio
async def test_call_tool_node_drift_get(mock_service):
    with patch.object(vstrike_module, "_get_service", return_value=mock_service):
        result = await vstrike_module.handle_call_tool(
            "vstrike_node_drift_get", {"node_id": "n1", "network_id": "net-1"}
        )
    data = json.loads(result[0].text)
    assert data["node_id"] == "n1"
    assert data["drift"] == [{"timestamp": "t1", "source": "cve"}]
    mock_service.node_drift_get.assert_called_once_with("n1", network_id="net-1")


@pytest.mark.asyncio
async def test_call_tool_storyline_list(mock_service):
    with patch.object(vstrike_module, "_get_service", return_value=mock_service):
        result = await vstrike_module.handle_call_tool(
            "vstrike_storyline_list", {"network_id": "net-1"}
        )
    data = json.loads(result[0].text)
    assert data["storylines"] == [{"storyline_id": "s1", "name": "Exfil"}]
    mock_service.storyline_list.assert_called_once_with(network_id="net-1")


@pytest.mark.asyncio
async def test_call_tool_storyline_events_get(mock_service):
    with patch.object(vstrike_module, "_get_service", return_value=mock_service):
        result = await vstrike_module.handle_call_tool(
            "vstrike_storyline_events_get", {"storyline_id": "s1", "network_id": "net-1"}
        )
    data = json.loads(result[0].text)
    assert data["storyline_id"] == "s1"
    assert data["events"] == [{"event_id": "e1", "timestamp": "t1"}]
    mock_service.storyline_events_get.assert_called_once_with("s1", network_id="net-1")


@pytest.mark.asyncio
async def test_call_tool_legend_run_list(mock_service):
    with patch.object(vstrike_module, "_get_service", return_value=mock_service):
        result = await vstrike_module.handle_call_tool(
            "vstrike_legend_run_list", {"network_id": "net-1"}
        )
    data = json.loads(result[0].text)
    assert data["legend_runs"] == [{"legend_run_id": "lr1", "name": "CVE"}]
    mock_service.legend_run_list.assert_called_once_with(network_id="net-1")


@pytest.mark.asyncio
async def test_call_tool_legend_run_results_get(mock_service):
    with patch.object(vstrike_module, "_get_service", return_value=mock_service):
        result = await vstrike_module.handle_call_tool(
            "vstrike_legend_run_results_get", {"legend_run_id": "lr1", "network_id": "net-1"}
        )
    data = json.loads(result[0].text)
    assert data["legend_run_id"] == "lr1"
    assert data["results"] == {"critical": 3}
    mock_service.legend_run_results_get.assert_called_once_with("lr1", network_id="net-1")


# ---------------------------------------------------------------------------
# Service unconfigured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_tool_returns_error_when_service_missing():
    with patch.object(vstrike_module, "_get_service", return_value=None):
        result = await vstrike_module.handle_call_tool(
            "vstrike_node_search", {"query": "router"}
        )
    data = json.loads(result[0].text)
    assert "error" in data
    assert "not configured" in data["error"].lower()
