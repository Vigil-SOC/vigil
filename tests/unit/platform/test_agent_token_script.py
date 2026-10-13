"""ensure_agent_token in scripts/lib.sh: an empty AGENT_INTERNAL_TOKEN in .env is
filled once, and a set one is never touched.

env.example ships the token empty, and scripts/agent_up.sh refuses to start the
agent layer without it. Drives the function from a temp copy of lib.sh, so
REPO_ROOT and its .env are temporary, with a stub `docker` first on PATH.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
HEX_TOKEN = re.compile(r'AGENT_INTERNAL_TOKEN="([0-9a-f]{64})"')


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    shutil.copy(REPO / "scripts" / "lib.sh", tmp_path / "scripts" / "lib.sh")
    (tmp_path / "bin").mkdir()
    docker = tmp_path / "bin" / "docker"
    docker.write_text("#!/bin/bash\nexit 0\n")
    docker.chmod(0o755)
    return tmp_path


def _ensure(root: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """Run ensure_agent_token under lib.sh's own `set -euo pipefail`, then print
    what it exported."""
    full_env = {k: v for k, v in os.environ.items() if k != "AGENT_INTERNAL_TOKEN"}
    full_env.update({"PATH": f"{root / 'bin'}:{os.environ['PATH']}", **env})
    return subprocess.run(
        [
            "bash",
            "-c",
            f'source "{root}/scripts/lib.sh"\nensure_agent_token\n'
            'echo "exported=${AGENT_INTERNAL_TOKEN:-}"',
        ],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _agent_up_reads(env_file: Path) -> str:
    """The token as scripts/agent_up.sh reads it: first line, quotes stripped."""
    for line in env_file.read_text().splitlines():
        if line.startswith("AGENT_INTERNAL_TOKEN="):
            value = line.split("=", 1)[1]
            for quote in ('"', "'"):
                value = value.removesuffix(quote).removeprefix(quote)
            return value
    return ""


def test_fills_the_empty_token_env_example_ships(sandbox: Path) -> None:
    env_file = sandbox / ".env"
    shutil.copy(REPO / "env.example", env_file)
    before = env_file.read_text().splitlines()

    result = _ensure(sandbox)

    assert result.returncode == 0, result.stderr
    after = env_file.read_text().splitlines()
    changed = [(a, b) for a, b in zip(before, after) if a != b]
    assert len(after) == len(before)
    assert [a for a, _ in changed] == ['AGENT_INTERNAL_TOKEN=""']
    token = HEX_TOKEN.fullmatch(changed[0][1]).group(1)
    assert _agent_up_reads(env_file) == token
    assert f"exported={token}" in result.stdout
    assert token not in result.stdout.replace(f"exported={token}", "")


@pytest.mark.parametrize(
    "line",
    [
        'AGENT_INTERNAL_TOKEN="kept"',
        "AGENT_INTERNAL_TOKEN=kept",
        "AGENT_INTERNAL_TOKEN='kept'",
    ],
)
def test_never_replaces_a_set_token(sandbox: Path, line: str) -> None:
    env_file = sandbox / ".env"
    env_file.write_text(f"DEV_MODE=false\n{line}\n")

    result = _ensure(sandbox)

    assert result.returncode == 0, result.stderr
    assert env_file.read_text() == f"DEV_MODE=false\n{line}\n"
    assert "AGENT_INTERNAL_TOKEN in .env" not in result.stdout


def test_a_second_run_keeps_the_first_token(sandbox: Path) -> None:
    env_file = sandbox / ".env"
    env_file.write_text("AGENT_INTERNAL_TOKEN=\n")
    assert _ensure(sandbox).returncode == 0
    first = env_file.read_text()
    assert HEX_TOKEN.fullmatch(first.strip())

    assert _ensure(sandbox).returncode == 0
    assert env_file.read_text() == first


def test_uses_a_token_the_caller_exported(sandbox: Path) -> None:
    env_file = sandbox / ".env"
    env_file.write_text('AGENT_INTERNAL_TOKEN=""\n')

    result = _ensure(sandbox, AGENT_INTERNAL_TOKEN="from-the-shell")

    assert result.returncode == 0, result.stderr
    assert env_file.read_text() == 'AGENT_INTERNAL_TOKEN="from-the-shell"\n'
    assert "exported=from-the-shell" in result.stdout


def test_appends_the_line_when_env_has_none(sandbox: Path) -> None:
    env_file = sandbox / ".env"
    env_file.write_text("DEV_MODE=false")  # no trailing newline either

    assert _ensure(sandbox).returncode == 0
    lines = env_file.read_text().splitlines()
    assert lines[0] == "DEV_MODE=false"
    assert HEX_TOKEN.fullmatch(lines[1])


def test_keeps_the_file_mode(sandbox: Path) -> None:
    env_file = sandbox / ".env"
    env_file.write_text('AGENT_INTERNAL_TOKEN=""\n')
    env_file.chmod(0o600)

    assert _ensure(sandbox).returncode == 0
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert not list(sandbox.glob(".env.tmp*"))


def test_no_env_file_is_left_alone(sandbox: Path) -> None:
    result = _ensure(sandbox)

    assert result.returncode == 0, result.stderr
    assert not (sandbox / ".env").exists()
    assert "exported=\n" in result.stdout


def test_setup_and_start_both_call_it() -> None:
    """setup_dev.sh for a fresh setup; start.sh for an install that skipped it,
    before anything that starts the agent layer."""
    assert "\nensure_agent_token\n" in (REPO / "setup_dev.sh").read_text()
    start = (REPO / "start.sh").read_text()
    assert start.index("\nensure_agent_token\n") < start.index(
        "\nstart_autostart_services\n"
    )
    assert start.index("\nensure_agent_token\n") < start.index(
        "    start_agent_layer\n"
    )
