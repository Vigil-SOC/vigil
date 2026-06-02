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
    svc.network_graph_get.return_value = {
        "label": "Prod-A",
        "nodes": [{"id": "n1"}],
        "edges": [],
        "bbox": {"x": 0, "y": 0, "w": 10, "h": 10},
    }
    svc.ui_legend_apply.return_value = {"ok": True}
    svc.ui_rightpanel_focus.return_value = {"ok": True}
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
        "vstrike_network_graph_get",
        "vstrike_ui_legend_apply",
        "vstrike_ui_rightpanel_focus",
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
            "vstrike_node_search",
            {"query": "router", "network_id": "net-1", "limit": 10},
        )
    data = json.loads(result[0].text)
    assert data["query"] == "router"
    assert data["results"] == [{"node_id": "n1", "node_name": "Router"}]
    mock_service.node_search.assert_called_once_with(
        "router", network_id="net-1", limit=10
    )


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
            "vstrike_storyline_events_get",
            {"storyline_id": "s1", "network_id": "net-1"},
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
            "vstrike_legend_run_results_get",
            {"legend_run_id": "lr1", "network_id": "net-1"},
        )
    data = json.loads(result[0].text)
    assert data["legend_run_id"] == "lr1"
    assert data["results"] == {"critical": 3}
    mock_service.legend_run_results_get.assert_called_once_with(
        "lr1", network_id="net-1"
    )


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


# ---------------------------------------------------------------------------
# Net-new VStrike tools (network-graph-get, ui-legend-apply,
# ui-rightpanel-focus)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_tool_network_graph_get(mock_service):
    with patch.object(vstrike_module, "_get_service", return_value=mock_service):
        result = await vstrike_module.handle_call_tool(
            "vstrike_network_graph_get", {"network_id": "net-1"}
        )
    data = json.loads(result[0].text)
    assert data["network_id"] == "net-1"
    assert data["graph"]["label"] == "Prod-A"
    assert data["graph"]["nodes"] == [{"id": "n1"}]
    mock_service.network_graph_get.assert_called_once_with(network_id="net-1")


@pytest.mark.asyncio
async def test_call_tool_network_graph_get_forwards_extras(mock_service):
    """Unknown args ride along verbatim to the service method."""
    with patch.object(vstrike_module, "_get_service", return_value=mock_service):
        await vstrike_module.handle_call_tool(
            "vstrike_network_graph_get",
            {"network_id": "net-1", "focusNodeId": "n9", "depth": 2},
        )
    kwargs = mock_service.network_graph_get.call_args.kwargs
    assert kwargs["network_id"] == "net-1"
    assert kwargs["focusNodeId"] == "n9"
    assert kwargs["depth"] == 2


@pytest.mark.asyncio
async def test_call_tool_network_graph_get_handles_failure(mock_service):
    mock_service.network_graph_get.return_value = None
    with patch.object(vstrike_module, "_get_service", return_value=mock_service):
        result = await vstrike_module.handle_call_tool(
            "vstrike_network_graph_get", {"network_id": "net-1"}
        )
    data = json.loads(result[0].text)
    assert "error" in data


@pytest.mark.asyncio
async def test_call_tool_ui_legend_apply(mock_service):
    with patch.object(vstrike_module, "_get_service", return_value=mock_service):
        result = await vstrike_module.handle_call_tool(
            "vstrike_ui_legend_apply",
            {"legend_run_id": "lr-1", "network_id": "net-1"},
        )
    data = json.loads(result[0].text)
    assert data["legend_run_id"] == "lr-1"
    assert data["network_id"] == "net-1"
    assert data["result"] == {"ok": True}
    mock_service.ui_legend_apply.assert_called_once_with("lr-1", network_id="net-1")


@pytest.mark.asyncio
async def test_call_tool_ui_legend_apply_requires_legend_run_id(mock_service):
    with patch.object(vstrike_module, "_get_service", return_value=mock_service):
        result = await vstrike_module.handle_call_tool(
            "vstrike_ui_legend_apply", {"network_id": "net-1"}
        )
    data = json.loads(result[0].text)
    assert "error" in data


@pytest.mark.asyncio
async def test_call_tool_ui_legend_apply_surfaces_runtime_error(mock_service):
    mock_service.ui_legend_apply.side_effect = RuntimeError("upstream boom")
    with patch.object(vstrike_module, "_get_service", return_value=mock_service):
        result = await vstrike_module.handle_call_tool(
            "vstrike_ui_legend_apply",
            {"legend_run_id": "lr-1", "network_id": "net-1"},
        )
    data = json.loads(result[0].text)
    assert "error" in data
    assert "upstream boom" in data["error"]


@pytest.mark.asyncio
async def test_call_tool_ui_rightpanel_focus_no_args(mock_service):
    """The tool takes no parameters; calling with an empty dict works."""
    with patch.object(vstrike_module, "_get_service", return_value=mock_service):
        result = await vstrike_module.handle_call_tool(
            "vstrike_ui_rightpanel_focus", {}
        )
    data = json.loads(result[0].text)
    assert data["result"] == {"ok": True}
    mock_service.ui_rightpanel_focus.assert_called_once_with()


@pytest.mark.asyncio
async def test_call_tool_ui_rightpanel_focus_forwards_extras(mock_service):
    """Unknown args ride through verbatim for forward compat."""
    with patch.object(vstrike_module, "_get_service", return_value=mock_service):
        await vstrike_module.handle_call_tool(
            "vstrike_ui_rightpanel_focus", {"future_field": "x"}
        )
    mock_service.ui_rightpanel_focus.assert_called_once_with(future_field="x")
