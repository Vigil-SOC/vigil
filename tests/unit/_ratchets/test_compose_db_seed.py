"""Compose reaches a schema only through the upgrade one-shot.

Postgres first boot does not apply infra/database/init. The one-shot runs
``python -m core.upgrade`` as the database owner after the pre-upgrade
snapshot, then sets vigil_app's password. ``restart: "no"`` so a refusal is
not replayed and the API is not started.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
COMPOSE_PATH = REPO / "infra" / "docker" / "docker-compose.yml"
ONESHOT = REPO / "infra" / "docker" / "upgrade-oneshot.sh"
APPLY_SH = REPO / "infra" / "docker" / "initdb" / "00_apply.sh"


def _services() -> dict:
    raw = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    return (raw or {}).get("services") or {}


def _volume_targets(spec: dict) -> dict[str, str]:
    """Map container path -> host path for bind mounts."""
    out: dict[str, str] = {}
    for entry in spec.get("volumes") or []:
        if not isinstance(entry, str) or ":" not in entry:
            continue
        host, container = entry.split(":", 1)
        container = container.split(":")[0]
        out[container] = host
    return out


def _entrypoint_text(spec: dict) -> str:
    ep = spec.get("entrypoint") or []
    if isinstance(ep, str):
        return ep
    return "\n".join(str(part) for part in ep)


def test_postgres_does_not_apply_init_sql_on_first_boot() -> None:
    mounts = _volume_targets(_services()["postgres"])
    assert "/docker-entrypoint-initdb.d" not in mounts
    assert "/db-init" not in mounts
    assert not list(
        (REPO / "infra" / "docker" / "initdb").glob("*.sql")
    ), "SQL under initdb/ would be run by Postgres with ON_ERROR_STOP=1"


def test_upgrade_one_shot_gates_the_api() -> None:
    assert "db-seed" not in _services()
    spec = _services()["upgrade"]
    assert not spec.get("profiles")
    assert spec.get("restart") == "no"
    assert not spec.get("ports")
    text = _entrypoint_text(spec)
    assert "/upgrade-oneshot.sh" in text
    mounts = _volume_targets(spec)
    assert mounts.get("/upgrade-oneshot.sh") == "./upgrade-oneshot.sh"
    assert mounts.get("/vigil-app-password.sh") == "./initdb/00_apply.sh"
    assert any(
        "/var/lib/vigil-upgrade" in str(entry) for entry in spec.get("volumes") or []
    )
    depends = spec.get("depends_on") or {}
    assert (depends.get("postgres") or {}).get("condition") == "service_healthy"
    assert (depends.get("backup-pre-upgrade") or {}).get("condition") == (
        "service_completed_successfully"
    )
    assert (depends.get("backend") or {}).get("condition") != "service_started"
    env = spec.get("environment") or {}
    assert env.get("POSTGRES_USER") == "deeptempo"
    assert env.get("VIGIL_UPGRADE_DIR") == "/var/lib/vigil-upgrade"
    script = ONESHOT.read_text(encoding="utf-8")
    assert "python -m core.upgrade --restore-point" in script
    assert "restore-point" in script
    assert "/vigil-app-password.sh" in script
    password = APPLY_SH.read_text(encoding="utf-8")
    assert "ALTER ROLE vigil_app" in password
    assert "/db-init" not in password
