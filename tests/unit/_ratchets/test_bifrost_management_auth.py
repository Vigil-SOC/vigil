"""Bifrost's management API is authenticated, and the secret is not a literal.

Issue #2573. Inference stays open: v2.2.1 gates that with
client.enforce_auth_on_inference, which is false here, and AuthConfig itself
has no inference switch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from core.llm.bifrost.auth import BIFROST_ADMIN_USERNAME

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
CONFIG = REPO / "infra" / "docker" / "bifrost" / "config.json"
COMPOSE = {
    "server": REPO / "infra" / "docker" / "docker-compose.yml",
    "desktop": REPO / "clients" / "desktop" / "standalone" / "docker-compose.yml",
}
# Processes that call /api/*. Agent processes call inference only.
NEEDS_PASSWORD = {
    "server": ("bifrost", "backend", "soc-daemon", "llm-worker"),
    "desktop": ("bifrost", "backend"),
}
MUST_NOT_HAVE = ("agent-worker", "agent-serve")


def _env(spec: dict) -> dict[str, str | None]:
    raw = spec.get("environment") or {}
    if isinstance(raw, list):
        parsed: dict[str, str | None] = {}
        for item in raw:
            key, sep, value = str(item).partition("=")
            parsed[key] = value if sep else None
        return parsed
    return {str(k): (None if v is None else str(v)) for k, v in raw.items()}


def test_config_enables_admin_auth_and_leaves_inference_open() -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    auth = config["governance"]["auth_config"]
    assert auth["is_enabled"] is True
    assert auth["admin_username"] == BIFROST_ADMIN_USERNAME
    password = auth["admin_password"]
    assert password.startswith("env.") and " " not in password
    assert not password.removeprefix("env.").isdigit()
    # The only inference switch v2.2.1 reads. Absent would also be off;
    # set false so a later default cannot close inference quietly.
    assert config["client"]["enforce_auth_on_inference"] is False


@pytest.mark.parametrize("stack", ["server", "desktop"])
def test_compose_passes_the_password_from_the_environment(stack: str) -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    var = config["governance"]["auth_config"]["admin_password"].removeprefix("env.")
    compose = yaml.safe_load(COMPOSE[stack].read_text(encoding="utf-8"))
    services = compose["services"]
    # The server file must render under `docker compose config` with a clean
    # environment (Medic's gate does that). `:?` would fail that render. An
    # empty value still does not open the API. Desktop is started only by the
    # app, which always injects the password, so an empty one is refused.
    expected = (
        f"${{{var}:?set by the desktop app}}"
        if stack == "desktop"
        else f"${{{var}:-}}"
    )
    for name in NEEDS_PASSWORD[stack]:
        value = _env(services[name]).get(var)
        assert value == expected, f"{stack} {name} password interpolation is {value}"
    for name in MUST_NOT_HAVE:
        assert var not in _env(services[name]), (
            f"{stack} {name} calls inference only and must not hold {var}"
        )


def test_host_scripts_send_the_same_username() -> None:
    user = BIFROST_ADMIN_USERNAME
    local = (REPO / "scripts" / "local_model.sh").read_text(encoding="utf-8")
    reset = (REPO / "scripts" / "reset-setup.sh").read_text(encoding="utf-8")
    assert f'-u "{user}:' in local
    assert 'f"' + user + ':{BF_PASS}"' in reset
