"""Default `docker compose up` is the investigation set; the daemon is opt-in.

Issue #902: soc-daemon (federation + auto-enrich) and llm-worker (ARQ drain)
are not on the incident-response path — workflows enqueue BullMQ `agent-runs`
to agent-worker and AGENT_URL points at agent-serve. Both sit behind one
`daemon` profile because the daemon's processor is the ARQ producer that
matters. agent-worker / agent-serve must never be profiled: a queued
investigation with no consumer is #868's class of bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
COMPOSE_PATH = REPO / "infra" / "docker" / "docker-compose.yml"

INVESTIGATION_SET = (
    "postgres",
    "db-seed",
    "redis",
    "bifrost",
    "backend",
    "agent-worker",
    "agent-serve",
)
DAEMON_SET = ("soc-daemon", "llm-worker")


def _services() -> dict:
    raw = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    return (raw or {}).get("services") or {}


@pytest.mark.parametrize("name", INVESTIGATION_SET)
def test_investigation_service_runs_on_default_up(name: str) -> None:
    spec = _services()[name]
    assert "profiles" not in spec, (
        f"{name} is behind a profile, so `docker compose up` will not start it"
    )


@pytest.mark.parametrize("name", DAEMON_SET)
def test_daemon_service_is_behind_daemon_profile_only(name: str) -> None:
    spec = _services()[name]
    assert spec.get("profiles") == ["daemon"], (
        f"{name} must be `profiles: [daemon]` exactly, got {spec.get('profiles')!r}"
    )
