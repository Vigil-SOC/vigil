"""Render Vigil's Compose files the way Docker sees them, for the Medic checks.

`docker compose config` does the merging, profile filtering and interpolation,
so these tests check what Docker would run rather than a hand-rolled reading of
the YAML. It needs the Docker CLI with Compose v2, not a running daemon.

The environment is built from scratch: an operator's COMPOSE_FILE,
COMPOSE_PROFILES or a sourced .env must not change what the gate sees.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMPOSE_DIR = REPO / "infra" / "docker"
BASE = COMPOSE_DIR / "docker-compose.yml"
# Enabling Medic on Compose takes both this overlay and `--profile medic` (S6-1).
OVERLAY = COMPOSE_DIR / "medic" / "docker-compose.medic.yml"
ENABLE = REPO / "scripts" / "medic" / "enable-compose.sh"

MEDIC_SERVICES = ("medic", "medic-gateway", "medic-dockerproxy")
# C3 §4.4, shape C. `backend` is deliberately absent (R1).
MEDIC_NET_MEMBERS = frozenset(
    {*MEDIC_SERVICES, "soc-daemon", "agent-worker", "agent-serve"}
)
NOT_ON_MEDIC_NET = ("backend", "redis", "postgres", "bifrost")
# K1 §6 C6 and C3 §7 check 2: none of these in any Medic-side environment.
FORBIDDEN_ENV = (
    "AGENT_INTERNAL_TOKEN",
    "VIGIL_TOOLS_TOKEN",
    "DAEMON_WEBHOOK_TOKEN",
    "VIGIL_MEDIC_API_KEY",
    "VIGIL_MEDIC_GATEWAY_VIEWER_PASSWORD",
    "POSTGRES_PASSWORD",
)

HAVE_COMPOSE = shutil.which("docker") is not None and (
    subprocess.run(
        ["docker", "compose", "version"], capture_output=True, check=False
    ).returncode
    == 0
)


def clean_env(home: Path, **extra: str) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home)}
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT"):
        if name in os.environ:
            env[name] = os.environ[name]
    # The CLI finds the Compose plugin (and its context) under the real config
    # dir, not the throwaway HOME.
    env["DOCKER_CONFIG"] = os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker"))
    env.update(extra)
    return env


def compose_cmd(
    *profiles: str, overlay: bool = True, files: tuple[Path, ...] = ()
) -> list[str]:
    cmd = ["docker", "compose", "--env-file", os.devnull, "-f", str(BASE)]
    if overlay:
        cmd += ["-f", str(OVERLAY)]
    for extra in files:
        cmd += ["-f", str(extra)]
    for profile in profiles:
        cmd += ["--profile", profile]
    return cmd


def render_raw(
    *profiles: str,
    home: Path,
    overlay: bool = True,
    base: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    cmd = compose_cmd(*profiles, overlay=overlay)
    if base is not None:
        # Same project directory, so relative paths and the project name resolve
        # exactly as they do for the real file.
        cmd[cmd.index(str(BASE))] = str(base)
        cmd[2:2] = ["--project-directory", str(COMPOSE_DIR)]
    return subprocess.run(
        [*cmd, "config", "--format", "json"],
        capture_output=True,
        text=True,
        env=clean_env(home, **(env or {})),
        check=False,
    )


def render(*profiles: str, home: Path, overlay: bool = True, **kw) -> dict:
    done = render_raw(*profiles, home=home, overlay=overlay, **kw)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def networks_of(spec: dict) -> set[str]:
    return set(spec.get("networks") or {})


def env_of(spec: dict) -> dict[str, str]:
    return dict(spec.get("environment") or {})
