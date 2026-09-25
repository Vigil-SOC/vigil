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

from core.intent import INTENT_FIELDS

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
COMPOSE_PATH = REPO / "infra" / "docker" / "docker-compose.yml"
HELM_VALUES_PATH = REPO / "infra" / "helm" / "vigil" / "values.yaml"

AGENT_URL = "http://agent-serve:6989"
AGENT_TOKEN = "${AGENT_INTERNAL_TOKEN:-}"

# Env names of INTENT_FIELDS. Listed once, on the x-intent-env anchor, and
# merged into both processes so the Settings card and the daemon agree.
INTENT_KNOBS = tuple(f.setting.upper() for f in INTENT_FIELDS)


def _env(service: str) -> dict[str, str | None]:
    """Service environment, whether compose stored it as a list or a mapping."""
    raw = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    env = ((raw or {}).get("services") or {}).get(service, {}).get("environment") or {}
    if isinstance(env, list):
        parsed: dict[str, str | None] = {}
        for item in env:
            key, sep, value = str(item).partition("=")
            parsed[key] = value if sep else None
        return parsed
    return {
        str(key): (None if value is None else str(value)) for key, value in env.items()
    }


def test_soc_daemon_shares_backend_agent_seam() -> None:
    backend = _env("backend")
    daemon = _env("soc-daemon")
    assert backend.get("AGENT_URL") == AGENT_URL
    assert backend.get("AGENT_INTERNAL_TOKEN") == AGENT_TOKEN
    assert (
        daemon.get("AGENT_URL") == AGENT_URL
    ), "soc-daemon is missing AGENT_URL; completed runs will never reconcile"
    assert (
        daemon.get("AGENT_INTERNAL_TOKEN") == AGENT_TOKEN
    ), "soc-daemon is missing AGENT_INTERNAL_TOKEN; projection reads will 401"


def test_backend_forwards_jwt_secret_and_dev_mode() -> None:
    # Issue #1096: without these a bare compose up crashloops the backend at
    # core/auth/auth_service.py. No default secret: unset must still fail closed.
    backend = _env("backend")
    assert backend.get("JWT_SECRET_KEY") == "${JWT_SECRET_KEY:-}"
    assert backend.get("DEV_MODE") == "${DEV_MODE:-false}"


def test_backend_and_daemon_share_one_intent_knob_list() -> None:
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    for key in INTENT_KNOBS:
        lines = [line for line in text.splitlines() if key in line]
        assert len(lines) == 1, f"{key} must be listed once, got {lines}"
    backend = _env("backend")
    daemon = _env("soc-daemon")
    for key in INTENT_KNOBS:
        assert key in backend and key in daemon
        assert backend[key] == daemon[key]


def test_helm_config_lists_every_intent_setting() -> None:
    values = yaml.safe_load(HELM_VALUES_PATH.read_text(encoding="utf-8"))
    config = (values or {}).get("config") or {}
    missing = [key for key in INTENT_KNOBS if key not in config]
    assert missing == []
