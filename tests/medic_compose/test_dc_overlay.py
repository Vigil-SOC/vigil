"""V1-4: Vigil's own compose wrapper keeps Medic once it is enabled.

`scripts/lib.sh dc` is how start.sh (and every script that sources lib.sh) runs
Compose. After `scripts/medic/enable-compose.sh` has written
`<secrets dir>/compose.env`, every `dc` call adds Medic's overlay and Medic's
settings, so a restart through start.sh can't drop the agents off medic-net
(Medic blind). Without that file, `dc` is exactly what it was.

A fake `docker` on PATH records each call's arguments and the VIGIL_MEDIC_*
environment it got; nothing here needs Docker.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit

FAKE_DOCKER = """#!/bin/bash
[ "$1 $2" = "compose version" ] && exit 0
{
  printf 'ARGS'; printf ' %s' "$@"; printf '\\n'
  env | grep -E '^(VIGIL_MEDIC_|COMPOSE_|LD_PRELOAD=)' | sort
} >> "$CALLS"
"""


@pytest.fixture
def sb(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "lib.sh", root / "scripts" / "lib.sh")
    (root / "infra" / "docker" / "medic").mkdir(parents=True)
    (root / "infra" / "docker" / "docker-compose.yml").write_text("services: {}\n")
    (root / "infra" / "docker" / "medic" / "docker-compose.medic.yml").write_text(
        "services: {}\n"
    )
    (tmp_path / "bin").mkdir()
    docker = tmp_path / "bin" / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    secrets = tmp_path / "home" / ".vigil-medic" / "secrets"
    secrets.mkdir(parents=True, mode=0o700)
    return tmp_path


def _compose_env(sb: Path, text: str, where: Path | None = None) -> Path:
    path = (where or sb / "home" / ".vigil-medic" / "secrets") / "compose.env"
    path.write_text(text)
    path.chmod(0o600)
    return path


ENABLED = (
    "# Medic on Compose: written by scripts/medic/enable-compose.sh.\n"
    "VIGIL_MEDIC_ENABLED=true\n"
    "VIGIL_MEDIC_SECRETS_DIR=/srv/medic-secrets\n"
    "VIGIL_MEDIC_DOCKER_GID=0\n"
    "VIGIL_MEDIC_VIEWER_USER=medic-abcdefghijkl\n"
)


def _dc(sb: Path, body: str, **extra: str) -> list[tuple[list[str], dict[str, str]]]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("VIGIL_", "COMPOSE_", "DOCKER_"))
    }
    env.update(
        PATH=f"{sb / 'bin'}:{os.environ['PATH']}",
        HOME=str(sb / "home"),
        CALLS=str(sb / "calls"),
        **extra,
    )
    done = subprocess.run(
        ["bash", "-c", f'source "{sb}/repo/scripts/lib.sh"\n{body}'],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    calls: list[tuple[list[str], dict[str, str]]] = []
    for line in (sb / "calls").read_text().splitlines():
        if line.startswith("ARGS"):
            calls.append((line.split()[1:], {}))
        else:
            k, _, v = line.partition("=")
            calls[-1][1][k] = v
    return calls


def _files(sb: Path) -> tuple[str, str]:
    d = sb / "repo" / "infra" / "docker"
    return str(d / "docker-compose.yml"), str(d / "medic" / "docker-compose.medic.yml")


def test_without_compose_env_dc_is_unchanged(sb: Path) -> None:
    base, _ = _files(sb)
    ((args, env),) = _dc(sb, "dc up -d postgres")
    assert args == ["compose", "-f", base, "up", "-d", "postgres"]
    assert env == {}


def test_enabled_dc_adds_the_overlay_and_medics_settings(sb: Path) -> None:
    base, overlay = _files(sb)
    _compose_env(sb, ENABLED)
    ((args, env),) = _dc(sb, "dc up -d agent-worker")
    assert args == ["compose", "-f", base, "-f", overlay, "up", "-d", "agent-worker"]
    assert env["VIGIL_MEDIC_ENABLED"] == "true"
    assert env["VIGIL_MEDIC_SECRETS_DIR"] == "/srv/medic-secrets"
    assert env["VIGIL_MEDIC_VIEWER_USER"] == "medic-abcdefghijkl"


def test_compose_env_beats_a_sourced_dotenv(sb: Path) -> None:
    """start.sh's load_env exports .env, and env.example ships the flag false.

    Shell env beats every --env-file, so the flag would render false and Medic
    would sit in its "off" loop (V1 review R1)."""
    _compose_env(sb, ENABLED)
    ((_, env),) = _dc(sb, "dc up -d agent-worker", VIGIL_MEDIC_ENABLED="false")
    assert env["VIGIL_MEDIC_ENABLED"] == "true"


def test_profile_and_project_selection_still_pass_through(sb: Path) -> None:
    _compose_env(sb, ENABLED)
    ((args, env),) = _dc(
        sb, "COMPOSE_PROFILES=daemon dc up -d soc-daemon", COMPOSE_PROJECT_NAME="x"
    )
    assert env["COMPOSE_PROFILES"] == "daemon" and env["COMPOSE_PROJECT_NAME"] == "x"
    assert args[-3:] == ["up", "-d", "soc-daemon"]


def test_secrets_dir_setting_is_honoured(sb: Path, tmp_path: Path) -> None:
    _, overlay = _files(sb)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    _compose_env(sb, ENABLED, where=elsewhere)
    ((args, _),) = _dc(sb, "dc ps", VIGIL_MEDIC_SECRETS_DIR=str(elsewhere))
    assert overlay in args
    # ...and the default location isn't consulted when it is set.
    ((args, _),) = _dc(
        sb, "dc ps", VIGIL_MEDIC_SECRETS_DIR=str(tmp_path / "nothing-here")
    )[-1:]
    assert overlay not in args


def test_only_medic_settings_are_taken_from_compose_env(sb: Path) -> None:
    """compose.env is Medic's file: it can't steer Compose or the shell."""
    base, overlay = _files(sb)
    _compose_env(
        sb,
        ENABLED
        + "COMPOSE_FILE=/tmp/evil.yml\nLD_PRELOAD=/tmp/evil.so\n"
        + "VIGIL_MEDIC_X=$(touch /tmp/s11-pwned)\nnot a line\n",
    )
    ((args, env),) = _dc(sb, "dc ps")
    assert args == ["compose", "-f", base, "-f", overlay, "ps"]
    assert "COMPOSE_FILE" not in env and "LD_PRELOAD" not in env
    # Taken as text, never run.
    assert env["VIGIL_MEDIC_X"] == "$(touch /tmp/s11-pwned)"


def test_overlay_missing_means_no_overlay(sb: Path) -> None:
    base, overlay = _files(sb)
    Path(overlay).unlink()
    _compose_env(sb, ENABLED)
    ((args, env),) = _dc(sb, "dc ps")
    assert args == ["compose", "-f", base, "ps"]
    assert env == {}


def test_operator_override_files_come_last(sb: Path, tmp_path: Path) -> None:
    base, overlay = _files(sb)
    _compose_env(sb, ENABLED)
    a, b = tmp_path / "a.yml", tmp_path / "b.yml"
    ((args, _),) = _dc(sb, "dc ps", VIGIL_MEDIC_COMPOSE_OVERRIDE=f"{a}:{b}")
    assert args == [
        "compose",
        "-f",
        base,
        "-f",
        overlay,
        "-f",
        str(a),
        "-f",
        str(b),
        "ps",
    ]


def _code(name: str) -> list[str]:
    return [ln.split("#", 1)[0] for ln in (REPO / name).read_text().splitlines()]


def test_start_sh_runs_compose_only_through_dc() -> None:
    """Every compose call on the start path goes through `dc` (V1-4)."""
    for line in _code("start.sh"):
        assert not any(
            raw in line for raw in ("docker compose", "docker-compose", "_DC_CMD")
        ), line
    # In lib.sh the raw command is expanded in exactly one place: dc itself.
    assert sum('"${_DC_CMD[@]}"' in line for line in _code("scripts/lib.sh")) == 1
