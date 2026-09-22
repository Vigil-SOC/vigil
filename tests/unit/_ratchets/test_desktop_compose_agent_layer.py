"""The desktop stack runs the agent layer, or investigations queue forever.

Issue #1015: clients/desktop/standalone/docker-compose.yml had nothing draining
BullMQ `agent-runs` and nothing answering AGENT_URL, so a workflow started from
the desktop app showed queued and never ran (#868's class of bug).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
COMPOSE_PATH = REPO / "clients" / "desktop" / "standalone" / "docker-compose.yml"

AGENT_SERVICES = ("agent-worker", "agent-serve")


def _services() -> dict:
    raw = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    return (raw or {}).get("services") or {}


@pytest.mark.parametrize("name", AGENT_SERVICES)
def test_agent_service_runs_on_default_up_without_host_port(name: str) -> None:
    spec = _services()[name]
    assert "profiles" not in spec, f"{name} is behind a profile, so `up` will not start it"
    assert "ports" not in spec, f"{name} must not publish a host port"


def test_backend_points_at_agent_serve() -> None:
    env = _services()["backend"]["environment"]
    assert "AGENT_URL=http://agent-serve:6989" in env
