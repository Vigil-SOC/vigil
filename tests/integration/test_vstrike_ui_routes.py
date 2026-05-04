"""Integration tests for the VStrike UI control plane routes.

These exercise:
  - POST /ui/iframe-token   → returns short-lived token + iframe URL
  - GET  /ui/networks       → returns network list
  - POST /ui/load-network   → triggers VStrike to update its iframe

The handlers resolve a `VStrikeService` via `get_vstrike_service()`. We
patch that factory to return a `MagicMock` so no live VStrike or HTTP is
needed.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT, ROOT / "backend"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("DEV_MODE", "true")


def _mock_ui_service(**overrides):
    svc = MagicMock()
    svc.base_url = overrides.get("base_url", "https://vstrike.example.com")
    svc.has_ui_credentials = overrides.get("has_ui_credentials", True)
    svc.has_api_credentials = overrides.get("has_api_credentials", True)
    svc.get_ui_login_token.return_value = overrides.get("ui_login_token", "tok-xyz")
    svc.list_networks.return_value = overrides.get(
        "networks", [{"id": "n-1", "name": "Prod"}]
    )
    svc.load_network_in_ui.return_value = overrides.get("load_result", {"ok": True})
    return svc


# --------------------------------------------------------------------------- #
# /ui/iframe-token
# --------------------------------------------------------------------------- #


def test_iframe_token_returns_token_and_url():
    from backend.api import vstrike as vstrike_module

    svc = _mock_ui_service(base_url="https://vstrike.net", ui_login_token="tok-abc")
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        result = asyncio.run(vstrike_module.ui_iframe_token())

    assert result["token"] == "tok-abc"
    assert result["iframe_url"] == "https://vstrike.net/login?token=tok-abc"
    svc.get_ui_login_token.assert_called_once_with()


def test_iframe_token_503_when_ui_credentials_missing():
    from backend.api import vstrike as vstrike_module

    svc = _mock_ui_service(has_ui_credentials=False)
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(vstrike_module.ui_iframe_token())

    assert exc_info.value.status_code == 503
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert "username" in detail["missing"]
    assert "password" in detail["missing"]


def test_iframe_token_503_when_service_not_configured():
    from backend.api import vstrike as vstrike_module

    with patch.object(vstrike_module, "get_vstrike_service", return_value=None):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(vstrike_module.ui_iframe_token())

    assert exc_info.value.status_code == 503


def test_iframe_token_502_when_upstream_fails():
    from backend.api import vstrike as vstrike_module

    svc = _mock_ui_service()
    svc.get_ui_login_token.side_effect = RuntimeError("upstream blew up")
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(vstrike_module.ui_iframe_token())

    assert exc_info.value.status_code == 502
    assert "upstream" in str(exc_info.value.detail)


# --------------------------------------------------------------------------- #
# /ui/networks
# --------------------------------------------------------------------------- #


def test_list_networks_returns_payload():
    from backend.api import vstrike as vstrike_module

    networks = [
        {"id": "n-1", "name": "Prod"},
        {"id": "n-2", "name": "Lab"},
    ]
    svc = _mock_ui_service(networks=networks)
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        result = asyncio.run(vstrike_module.ui_list_networks())

    assert result == {"networks": networks}


def test_list_networks_503_without_ui_credentials():
    from backend.api import vstrike as vstrike_module

    svc = _mock_ui_service(has_ui_credentials=False)
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(vstrike_module.ui_list_networks())

    assert exc_info.value.status_code == 503


# --------------------------------------------------------------------------- #
# /ui/load-network
# --------------------------------------------------------------------------- #


def test_load_network_calls_service_with_network_id():
    from backend.api import vstrike as vstrike_module
    from backend.api.vstrike import VStrikeLoadNetworkRequest

    svc = _mock_ui_service()
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        result = asyncio.run(
            vstrike_module.ui_load_network(
                VStrikeLoadNetworkRequest(network_id="net-42")
            )
        )

    assert result["ok"] is True
    svc.load_network_in_ui.assert_called_once_with("net-42")


def test_load_network_502_when_upstream_fails():
    from backend.api import vstrike as vstrike_module
    from backend.api.vstrike import VStrikeLoadNetworkRequest

    svc = _mock_ui_service()
    svc.load_network_in_ui.side_effect = RuntimeError("connection refused")
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                vstrike_module.ui_load_network(
                    VStrikeLoadNetworkRequest(network_id="n")
                )
            )

    assert exc_info.value.status_code == 502
    assert "connection refused" in str(exc_info.value.detail)


# --------------------------------------------------------------------------- #
# /ui/killchain-replay
# --------------------------------------------------------------------------- #


def _make_killchain_request(**overrides):
    from backend.api.vstrike import (
        VStrikeKillchainReplayRequest,
        VStrikeKillchainStep,
    )

    return VStrikeKillchainReplayRequest(
        network_id=overrides.get("network_id", "net-1"),
        steps=overrides.get(
            "steps",
            [
                VStrikeKillchainStep(
                    node_id="asset-001",
                    timestamp="2026-04-28T11:00:00Z",
                    label="Initial Access",
                ),
                VStrikeKillchainStep(
                    node_id="asset-077",
                    timestamp="2026-04-28T11:12:00Z",
                    technique="T1003.001",
                    label="Target",
                ),
            ],
        ),
        loop=overrides.get("loop", False),
        auto_play=overrides.get("auto_play", True),
    )


def test_killchain_replay_passes_steps_to_service():
    from backend.api import vstrike as vstrike_module

    svc = _mock_ui_service()
    svc.killchain_replay_in_ui.return_value = {"ok": True, "queued": 2}
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        result = asyncio.run(
            vstrike_module.ui_killchain_replay(_make_killchain_request())
        )

    assert result["ok"] is True
    svc.killchain_replay_in_ui.assert_called_once()
    args, kwargs = svc.killchain_replay_in_ui.call_args
    assert args[0] == "net-1"
    steps = args[1]
    assert [s["node_id"] for s in steps] == ["asset-001", "asset-077"]
    # exclude_none must drop unset optional fields.
    assert "technique" not in steps[0]
    assert steps[1]["technique"] == "T1003.001"
    assert kwargs == {"loop": False, "auto_play": True}


def test_killchain_replay_501_when_tool_not_implemented():
    from backend.api import vstrike as vstrike_module
    from services.vstrike_service import VStrikeToolNotImplemented

    svc = _mock_ui_service()
    svc.killchain_replay_in_ui.side_effect = VStrikeToolNotImplemented(
        "VStrike server does not yet implement ui-killchain-replay."
    )
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(vstrike_module.ui_killchain_replay(_make_killchain_request()))

    assert exc_info.value.status_code == 501
    assert "ui-killchain-replay" in str(exc_info.value.detail)


def test_killchain_replay_502_on_other_runtime_errors():
    from backend.api import vstrike as vstrike_module

    svc = _mock_ui_service()
    svc.killchain_replay_in_ui.side_effect = RuntimeError("transport failed")
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(vstrike_module.ui_killchain_replay(_make_killchain_request()))

    assert exc_info.value.status_code == 502
    assert "transport failed" in str(exc_info.value.detail)


def test_killchain_replay_503_without_ui_credentials():
    from backend.api import vstrike as vstrike_module

    svc = _mock_ui_service(has_ui_credentials=False)
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(vstrike_module.ui_killchain_replay(_make_killchain_request()))

    assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# Data-plane proxies (node search, drift, storylines, legends)
# ---------------------------------------------------------------------------


def _mock_data_service(**overrides):
    svc = _mock_ui_service(**overrides)
    svc.node_search.return_value = overrides.get(
        "node_search", [{"node_id": "n1", "node_name": "Router-A"}]
    )
    svc.node_drift_get.return_value = overrides.get(
        "node_drift", [{"timestamp": "t1", "source": "cve"}]
    )
    svc.storyline_list.return_value = overrides.get(
        "storylines", [{"storyline_id": "s1", "name": "Exfil"}]
    )
    svc.storyline_events_get.return_value = overrides.get(
        "storyline_events", [{"event_id": "e1", "timestamp": "t1"}]
    )
    svc.legend_run_list.return_value = overrides.get(
        "legend_runs", [{"legend_run_id": "lr1", "name": "CVE-2026-001"}]
    )
    svc.legend_run_results_get.return_value = overrides.get(
        "legend_results", {"legend_run_id": "lr1", "results": {"critical": 3}}
    )
    return svc


def test_node_search_returns_results():
    from backend.api import vstrike as vstrike_module
    from backend.api.vstrike import VStrikeNodeSearchRequest

    svc = _mock_data_service()
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        result = asyncio.run(
            vstrike_module.node_search(VStrikeNodeSearchRequest(query="router", network_id="net-1", limit=10))
        )

    assert result["query"] == "router"
    assert result["results"] == [{"node_id": "n1", "node_name": "Router-A"}]
    svc.node_search.assert_called_once_with("router", network_id="net-1", limit=10)


def test_node_search_503_without_ui_credentials():
    from backend.api import vstrike as vstrike_module
    from backend.api.vstrike import VStrikeNodeSearchRequest

    svc = _mock_data_service(has_ui_credentials=False)
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(vstrike_module.node_search(VStrikeNodeSearchRequest(query="x")))

    assert exc_info.value.status_code == 503


def test_node_drift_returns_drift():
    from backend.api import vstrike as vstrike_module
    from backend.api.vstrike import VStrikeNodeDriftRequest

    svc = _mock_data_service()
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        result = asyncio.run(
            vstrike_module.node_drift(VStrikeNodeDriftRequest(node_id="node-1", network_id="net-1"))
        )

    assert result["node_id"] == "node-1"
    assert result["drift"] == [{"timestamp": "t1", "source": "cve"}]
    svc.node_drift_get.assert_called_once_with("node-1", network_id="net-1")


def test_list_storylines_returns_storylines():
    from backend.api import vstrike as vstrike_module

    svc = _mock_data_service()
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        result = asyncio.run(vstrike_module.list_storylines("net-1"))

    assert result["storylines"] == [{"storyline_id": "s1", "name": "Exfil"}]
    svc.storyline_list.assert_called_once_with(network_id="net-1")


def test_storyline_events_returns_events():
    from backend.api import vstrike as vstrike_module
    from backend.api.vstrike import VStrikeStorylineEventsRequest

    svc = _mock_data_service()
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        result = asyncio.run(
            vstrike_module.storyline_events(VStrikeStorylineEventsRequest(storyline_id="s1", network_id="net-1"))
        )

    assert result["storyline_id"] == "s1"
    assert result["events"] == [{"event_id": "e1", "timestamp": "t1"}]
    svc.storyline_events_get.assert_called_once_with("s1", network_id="net-1")


def test_list_legend_runs_returns_runs():
    from backend.api import vstrike as vstrike_module

    svc = _mock_data_service()
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        result = asyncio.run(vstrike_module.list_legend_runs("net-1"))

    assert result["legend_runs"] == [{"legend_run_id": "lr1", "name": "CVE-2026-001"}]
    svc.legend_run_list.assert_called_once_with(network_id="net-1")


def test_legend_run_results_returns_results():
    from backend.api import vstrike as vstrike_module
    from backend.api.vstrike import VStrikeLegendRunResultsRequest

    svc = _mock_data_service()
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        result = asyncio.run(
            vstrike_module.legend_run_results(VStrikeLegendRunResultsRequest(legend_run_id="lr1", network_id="net-1"))
        )

    assert result["legend_run_id"] == "lr1"
    assert result["results"] == {"legend_run_id": "lr1", "results": {"critical": 3}}
    svc.legend_run_results_get.assert_called_once_with("lr1", network_id="net-1")


# ---------------------------------------------------------------------------
# UI control plane (camera, storyline, VCR playback)
# ---------------------------------------------------------------------------


def _mock_ui_control_service(**overrides):
    svc = _mock_ui_service(**overrides)
    svc.ui_camera_node.return_value = overrides.get("camera_node", {"ok": True})
    svc.ui_camera_position.return_value = overrides.get("camera_position", {"ok": True})
    svc.ui_storyline_apply.return_value = overrides.get("storyline_apply", {"ok": True})
    svc.ui_storyline_mode.return_value = overrides.get("storyline_mode", {"ok": True})
    svc.ui_storyline_forward.return_value = overrides.get("storyline_forward", {"ok": True})
    svc.ui_storyline_backward.return_value = overrides.get("storyline_backward", {"ok": True})
    return svc


def test_ui_camera_node_calls_service():
    from backend.api import vstrike as vstrike_module
    from backend.api.vstrike import VStrikeCameraNodeRequest

    svc = _mock_ui_control_service()
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        result = asyncio.run(
            vstrike_module.ui_camera_node(VStrikeCameraNodeRequest(node_ids=["n1", "n2"], network_id="net-1"))
        )

    assert result["ok"] is True
    svc.ui_camera_node.assert_called_once_with(["n1", "n2"], network_id="net-1")


def test_ui_camera_position_calls_service():
    from backend.api import vstrike as vstrike_module
    from backend.api.vstrike import VStrikeCameraPositionRequest

    svc = _mock_ui_control_service()
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        result = asyncio.run(
            vstrike_module.ui_camera_position(
                VStrikeCameraPositionRequest(
                    position={"x": 1.0, "y": 2.0, "z": 3.0},
                    rotation={"pitch": 0.5},
                    network_id="net-1",
                )
            )
        )

    assert result["ok"] is True
    svc.ui_camera_position.assert_called_once_with(
        {"x": 1.0, "y": 2.0, "z": 3.0},
        rotation={"pitch": 0.5},
        network_id="net-1",
    )


def test_ui_storyline_apply_calls_service():
    from backend.api import vstrike as vstrike_module
    from backend.api.vstrike import VStrikeStorylineApplyRequest

    svc = _mock_ui_control_service()
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        result = asyncio.run(
            vstrike_module.ui_storyline_apply(VStrikeStorylineApplyRequest(storyline_id="s1", network_id="net-1"))
        )

    assert result["ok"] is True
    svc.ui_storyline_apply.assert_called_once_with("s1", network_id="net-1")


def test_ui_storyline_mode_calls_service():
    from backend.api import vstrike as vstrike_module
    from backend.api.vstrike import VStrikeStorylineModeRequest

    svc = _mock_ui_control_service()
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        result = asyncio.run(
            vstrike_module.ui_storyline_mode(VStrikeStorylineModeRequest(mode="replay", network_id="net-1"))
        )

    assert result["ok"] is True
    svc.ui_storyline_mode.assert_called_once_with("replay", network_id="net-1")


def test_ui_storyline_forward_calls_service():
    from backend.api import vstrike as vstrike_module

    svc = _mock_ui_control_service()
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        result = asyncio.run(vstrike_module.ui_storyline_forward("net-1"))

    assert result["ok"] is True
    svc.ui_storyline_forward.assert_called_once_with(network_id="net-1")


def test_ui_storyline_backward_calls_service():
    from backend.api import vstrike as vstrike_module

    svc = _mock_ui_control_service()
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        result = asyncio.run(vstrike_module.ui_storyline_backward("net-1"))

    assert result["ok"] is True
    svc.ui_storyline_backward.assert_called_once_with(network_id="net-1")


def test_ui_camera_node_501_when_tool_not_implemented():
    from backend.api import vstrike as vstrike_module
    from backend.api.vstrike import VStrikeCameraNodeRequest
    from services.vstrike_service import VStrikeToolNotImplemented

    svc = _mock_ui_control_service()
    svc.ui_camera_node.side_effect = VStrikeToolNotImplemented("ui-camera-node not implemented")
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(vstrike_module.ui_camera_node(VStrikeCameraNodeRequest(node_ids=["n1"])))

    assert exc_info.value.status_code == 501


def test_ui_storyline_forward_502_on_runtime_error():
    from backend.api import vstrike as vstrike_module

    svc = _mock_ui_control_service()
    svc.ui_storyline_forward.side_effect = RuntimeError("websocket closed")
    with patch.object(vstrike_module, "get_vstrike_service", return_value=svc):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(vstrike_module.ui_storyline_forward("net-1"))

    assert exc_info.value.status_code == 502
    assert "websocket closed" in str(exc_info.value.detail)
