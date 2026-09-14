"""soc-daemon must reach agent-serve the same way the backend does.

Issue #868: compose gave backend AGENT_URL and AGENT_INTERNAL_TOKEN but not
soc-daemon. core/config.py defaults agent_url to localhost:6989, which is the
host-run address; inside the compose network _read_fold cannot reach
agent-serve, returns None, and Orchestrator._reconcile leaves investigations
on executing. Even with the URL, serve 401s without the token.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
COMPOSE_PATH = REPO / "infra" / "docker" / "docker-compose.yml"

AGENT_URL = "AGENT_URL=http://agent-serve:6989"
AGENT_TOKEN = "AGENT_INTERNAL_TOKEN=${AGENT_INTERNAL_TOKEN:-}"


def _env(service: str) -> list[str]:
    raw = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    return list(
        ((raw or {}).get("services") or {}).get(service, {}).get("environment") or []
    )


def test_soc_daemon_shares_backend_agent_seam() -> None:
    backend = _env("backend")
    daemon = _env("soc-daemon")
    assert AGENT_URL in backend
    assert AGENT_TOKEN in backend
    assert (
        AGENT_URL in daemon
    ), "soc-daemon is missing AGENT_URL; completed runs will never reconcile"
    assert (
        AGENT_TOKEN in daemon
    ), "soc-daemon is missing AGENT_INTERNAL_TOKEN; projection reads will 401"
