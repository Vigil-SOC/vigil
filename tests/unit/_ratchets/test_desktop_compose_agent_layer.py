"""The desktop stack runs the agent layer, or investigations queue forever.

Issue #1015: clients/desktop/standalone/docker-compose.yml had nothing draining
BullMQ `agent-runs` and nothing answering AGENT_URL, so a workflow started from
the desktop app showed queued and never ran (#868's class of bug).
"""

from __future__ import annotations

from pathlib import Path

import json
import posixpath

import pytest
import yaml

from tests.unit._ratchets.test_compose_state_volumes import _named_volume_at

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
COMPOSE_PATH = REPO / "clients" / "desktop" / "standalone" / "docker-compose.yml"

# prepare-standalone.js copies this file to the desktop's bifrost-config.json.
BIFROST_CONFIG = REPO / "infra" / "docker" / "bifrost" / "config.json"

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


@pytest.mark.parametrize(
    "path",
    [
        REPO / "infra" / "docker" / "docker-compose.yml",
        COMPOSE_PATH,
    ],
    ids=["server", "desktop"],
)
def test_bifrost_request_log_lands_in_a_named_volume(path: Path) -> None:
    """logs_store.path resolves under a volume, not the writable layer (#1454).

    Bifrost's working directory is /app, so the relative "./logs.db" landed in
    /app/logs.db and every recreate deleted the gateway log.
    """
    compose = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    logs_path = json.loads(BIFROST_CONFIG.read_text(encoding="utf-8"))["logs_store"][
        "config"
    ]["path"]
    resolved = posixpath.normpath(posixpath.join("/app", logs_path))
    for name, spec in compose["services"].items():
        if "maximhq/bifrost" not in str(spec.get("image", "")):
            continue
        volume_dirs = [
            target
            for target in ("/app/data", "/app")
            if _named_volume_at(compose, name, target)
        ]
        assert any(resolved.startswith(f"{d}/") for d in volume_dirs), (
            f"{name} in {path.name} writes its request log to {resolved}, "
            "outside any named volume, so it is lost when the container is removed"
        )


_OWNER = "${POSTGRES_PASSWORD:?set by the desktop app}"
_APP = "${POSTGRES_APP_PASSWORD:?set by the desktop app}"


def _env_pairs(env: object) -> list[tuple[str, str]]:
    if isinstance(env, dict):
        return [(str(key), str(value)) for key, value in env.items()]
    if not isinstance(env, list):
        return []
    pairs: list[tuple[str, str]] = []
    for item in env:
        key, value = str(item).split("=", 1)
        pairs.append((key, value))
    return pairs


def test_postgres_password_is_minted_by_the_app() -> None:
    """Owner and vigil_app passwords are distinct per-install secrets (#2570).

    A literal here is a published default: any container on the network could
    log in as the superuser, and the backend logs an ERROR the user cannot fix.
    vigil_app's password is a second minted secret. App containers receive only
    that one, so reading their environment does not authenticate as the owner.
    """
    raw = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8")) or {}
    envs = {"x-agent-env": raw["x-agent-env"]}
    envs.update({n: s.get("environment") or {} for n, s in raw["services"].items()})
    owner_slots = 0
    app_slots = 0
    for name, env in envs.items():
        pairs = dict(_env_pairs(env))
        user = pairs.get("POSTGRES_USER") or pairs.get("PGUSER")
        if user == "vigil_app" or name == "x-agent-env":
            assert pairs.get("POSTGRES_PASSWORD") == _APP, name
            assert _OWNER not in pairs.values(), name
            app_slots += 1
        if user == "deeptempo":
            owner_value = pairs.get("PGPASSWORD") or pairs.get("POSTGRES_PASSWORD")
            assert owner_value == _OWNER, name
            assert pairs.get("POSTGRES_APP_PASSWORD") == _APP, name
            owner_slots += 1
    assert app_slots >= 3, "expected the agent env, backend and anchor"
    assert owner_slots >= 2, "expected postgres and db-seed"
