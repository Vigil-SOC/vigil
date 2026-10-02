"""The backup schedule is on the default project and uses the cluster owner.

`vigil_app` cannot CREATE DATABASE, so the service must connect as the
postgres service's user. It mounts the same state, workdir, and Bifrost
volumes as the API. It is not on the daemon profile. The command is the
schedule loop and the restart policy brings it back if the process exits.
`start.sh backup` and `start.sh restore` override that command with one run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
COMPOSE_PATH = REPO / "infra" / "docker" / "docker-compose.yml"

STATE_DIR = "/home/vigil/.vigil"
INVESTIGATIONS_DIR = "/app/data/investigations"
BIFROST_DIR = "/var/lib/vigil/bifrost"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8")) or {}


def _env(spec: dict) -> dict[str, str]:
    raw = spec.get("environment") or {}
    if isinstance(raw, list):
        parsed: dict[str, str] = {}
        for item in raw:
            key, sep, value = str(item).partition("=")
            if sep:
                parsed[key] = value
        return parsed
    return {str(key): "" if value is None else str(value) for key, value in raw.items()}


def _default_source(source: str) -> str:
    if source.startswith("${") and source.endswith("}") and ":-" in source:
        return source.split(":-", 1)[1][:-1]
    return source


def _mounts(spec: dict) -> dict[str, str]:
    """Container path -> volume source, with ${VAR:-default} reduced to default."""
    found: dict[str, str] = {}
    for entry in spec.get("volumes") or []:
        if not isinstance(entry, str):
            continue
        body = entry
        if body.endswith(":ro") or body.endswith(":rw"):
            body = body.rsplit(":", 1)[0]
        if ":" not in body:
            continue
        source, target = body.rsplit(":", 1)
        if target.startswith("/"):
            found[target] = _default_source(source)
    return found


def _command(spec: dict) -> str:
    parts = spec.get("entrypoint") or spec.get("command") or []
    if isinstance(parts, str):
        return parts
    return "\n".join(str(part) for part in parts)


def test_backup_mounts_state_workdirs_and_owner_user() -> None:
    compose = _compose()
    services = compose["services"]
    backup = services["backup"]
    assert not backup.get(
        "profiles"
    ), "backup is behind a profile, so a default `up` will not define it"
    assert "daemon" not in (backup.get("profiles") or [])
    mounts = _mounts(backup)
    assert mounts.get(STATE_DIR) == "vigil_home"
    assert mounts.get(INVESTIGATIONS_DIR) == "vigil_investigations"
    assert mounts.get(BIFROST_DIR) == "bifrost_data"
    postgres_user = _env(services["postgres"])["POSTGRES_USER"]
    backup_env = _env(backup)
    assert backup_env["POSTGRES_USER"] == postgres_user
    assert (
        backup_env["POSTGRES_PASSWORD"]
        == _env(services["postgres"])["POSTGRES_PASSWORD"]
    )
    assert backup_env["POSTGRES_DB"] == _env(services["postgres"])["POSTGRES_DB"]
    assert backup_env["HOME"] == "/home/vigil"
    assert "deeptempo-network" in (backup.get("networks") or [])
    assert backup.get("restart") == "unless-stopped"
    assert (backup.get("healthcheck") or {}).get("disable") is True
    command = _command(backup)
    assert "python -m core.backup run" in command
    assert f"--bifrost-data {BIFROST_DIR}" in command


def test_start_sh_overrides_the_loop_with_one_create_or_restore() -> None:
    text = (REPO / "start.sh").read_text(encoding="utf-8")
    assert (
        "\n".join(
            [
                "--entrypoint python backup -m core.backup \"$sub\"",
                "        --repo /backup/repo --passphrase-file /backup/passphrase",
                "        --bifrost-data /var/lib/vigil/bifrost",
            ]
        )
        in text
    )
    assert "prepare_backup_run backup " in text
    assert "prepare_backup_run restore " in text
