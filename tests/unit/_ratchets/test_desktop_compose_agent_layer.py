"""The desktop stack runs the agent layer, or investigations queue forever.

Issue #1015: clients/desktop/standalone/docker-compose.yml had nothing draining
BullMQ `agent-runs` and nothing answering AGENT_URL, so a workflow started from
the desktop app showed queued and never ran (#868's class of bug).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.unit._ratchets.test_compose_state_volumes import _named_volume_at

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
    assert (
        "profiles" not in spec
    ), f"{name} is behind a profile, so `up` will not start it"
    assert "ports" not in spec, f"{name} must not publish a host port"


def test_backend_points_at_agent_serve() -> None:
    env = _services()["backend"]["environment"]
    assert "AGENT_URL=http://agent-serve:6989" in env


@pytest.mark.parametrize(
    "path",
    [
        REPO / "infra" / "docker" / "docker-compose.yml",
        COMPOSE_PATH,
    ],
    ids=["server", "desktop"],
)
def test_bifrost_keeps_runtime_config_in_a_named_volume(path: Path) -> None:
    """Bifrost's config.db (keys, virtual keys, budgets) lives in /app/data (#1452)."""
    compose = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    bifrost = [
        name
        for name, spec in compose["services"].items()
        if "maximhq/bifrost" in str(spec.get("image", ""))
    ]
    assert bifrost, f"{path} runs no maximhq/bifrost service"
    for name in bifrost:
        assert _named_volume_at(compose, name, "/app/data"), (
            f"{name} in {path.name} has no named volume at /app/data, so its "
            "settings are lost when the container is removed"
        )
