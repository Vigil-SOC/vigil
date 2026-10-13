"""ensure_agent_token and env_file_value in scripts/lib.sh.

env.example ships AGENT_INTERNAL_TOKEN empty, and scripts/agent_up.sh refuses to
start the agent layer without it. ensure_agent_token fills an empty one once and
never touches a set one. env_file_value is how agent_up.sh reads it back: as
sourcing .env would, so the agent layer and the backend agree on the value.

Drives the functions from a temp copy of scripts/, so REPO_ROOT and its .env are
temporary, with a stub `docker` first on PATH.
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
    for name in ("lib.sh", "agent_up.sh"):
        shutil.copy(REPO / "scripts" / name, tmp_path / "scripts" / name)
    (tmp_path / "bin").mkdir()
    _stub(tmp_path, "docker", "exit 0")
    return tmp_path


def _stub(root: Path, name: str, body: str) -> None:
    path = root / "bin" / name
    path.write_text(f"#!/bin/bash\n{body}\n")
    path.chmod(0o755)


def _bash(root: Path, body: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Run ``body`` after sourcing lib.sh, so under its `set -euo pipefail`."""
    full_env = {k: v for k, v in os.environ.items() if k != "AGENT_INTERNAL_TOKEN"}
    full_env.update({"PATH": f"{root / 'bin'}:{os.environ['PATH']}", **env})
    return subprocess.run(
        ["bash", "-c", f'source "{root}/scripts/lib.sh"\n{body}'],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _ensure(root: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """ensure_agent_token, then what it exported."""
    return _bash(
        root, 'ensure_agent_token\necho "exported=${AGENT_INTERNAL_TOKEN:-}"', **env
    )


def _value(root: Path) -> str:
    """The token as agent_up.sh reads it."""
    return _bash(root, "env_file_value AGENT_INTERNAL_TOKEN").stdout


def _sourced(root: Path) -> str:
    """The token as start.sh's load_env (and so the backend) gets it."""
    return _bash(root, 'load_env\nprintf %s "$AGENT_INTERNAL_TOKEN"').stdout


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
    assert _value(sandbox) == _sourced(sandbox) == token
    assert f"exported={token}" in result.stdout
    assert token not in result.stdout.replace(f"exported={token}", "")


@pytest.mark.parametrize(
    "line",
    [
        'AGENT_INTERNAL_TOKEN="kept"',
        "AGENT_INTERNAL_TOKEN=kept",
        "AGENT_INTERNAL_TOKEN='kept'",
        'export AGENT_INTERNAL_TOKEN="kept"',
        '  AGENT_INTERNAL_TOKEN="kept"',
        'AGENT_INTERNAL_TOKEN="kept"  # rotate yearly',
        'AGENT_INTERNAL_TOKEN="kept"\r',
    ],
)
def test_never_replaces_or_shadows_a_set_token(sandbox: Path, line: str) -> None:
    env_file = sandbox / ".env"
    env_file.write_text(f"DEV_MODE=false\n{line}\n", newline="")

    result = _ensure(sandbox)

    assert result.returncode == 0, result.stderr
    assert env_file.read_bytes() == f"DEV_MODE=false\n{line}\n".encode()
    assert "AGENT_INTERNAL_TOKEN in .env" not in result.stdout
    assert _value(sandbox) == "kept"


@pytest.mark.parametrize(
    "line, kept_prefix",
    [
        ('export AGENT_INTERNAL_TOKEN=""', "export "),
        ('  AGENT_INTERNAL_TOKEN=""', "  "),
        ('AGENT_INTERNAL_TOKEN=""  # set by setup', ""),
    ],
)
def test_fills_an_empty_line_in_any_form(sandbox, line, kept_prefix) -> None:
    env_file = sandbox / ".env"
    env_file.write_text(f"{line}\nDEV_MODE=false\n")

    assert _ensure(sandbox).returncode == 0

    first, second = env_file.read_text().splitlines()
    assert first.startswith(kept_prefix)
    assert HEX_TOKEN.fullmatch(first[len(kept_prefix) :])
    assert second == "DEV_MODE=false"
    assert _value(sandbox) == _sourced(sandbox) != ""


def test_keeps_crlf_line_endings(sandbox: Path) -> None:
    env_file = sandbox / ".env"
    env_file.write_bytes(b'DEV_MODE=false\r\nAGENT_INTERNAL_TOKEN=""\r\n')

    assert _ensure(sandbox).returncode == 0

    raw = env_file.read_bytes()
    assert raw.startswith(b"DEV_MODE=false\r\n") and raw.endswith(b'"\r\n')
    assert re.fullmatch(r"[0-9a-f]{64}", _value(sandbox))


@pytest.mark.parametrize(
    "lines",
    [
        'AGENT_INTERNAL_TOKEN=""\nAGENT_INTERNAL_TOKEN="mine"\n',
        'AGENT_INTERNAL_TOKEN=""\nexport AGENT_INTERNAL_TOKEN=""\n',
    ],
)
def test_two_lines_are_left_alone_with_a_warning(sandbox: Path, lines: str) -> None:
    env_file = sandbox / ".env"
    env_file.write_text(lines)

    result = _ensure(sandbox)

    assert result.returncode == 0, result.stderr
    assert env_file.read_text() == lines
    assert "sets AGENT_INTERNAL_TOKEN 2 times" in result.stderr


def test_agent_up_reads_the_line_sourcing_keeps(sandbox: Path) -> None:
    """An empty line first and the real one last: load_env exports the last, so
    agent_up.sh must too, or every /internal call answers 401."""
    (sandbox / ".env").write_text(
        'AGENT_INTERNAL_TOKEN=""\nexport AGENT_INTERNAL_TOKEN="mine"\n'
    )
    assert _value(sandbox) == _sourced(sandbox) == "mine"

    # agent_up.sh gets past its token check and stops at the missing
    # services/agent of this sandbox.
    result = subprocess.run(
        ["bash", str(sandbox / "scripts" / "agent_up.sh")],
        env={**os.environ, "PATH": f"{sandbox / 'bin'}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "AGENT_INTERNAL_TOKEN is unset" not in result.stderr
    assert "services/agent" in result.stderr


def test_agent_up_still_refuses_an_empty_token(sandbox: Path) -> None:
    (sandbox / ".env").write_text('AGENT_INTERNAL_TOKEN=""  # fill me\n')
    result = subprocess.run(
        ["bash", str(sandbox / "scripts" / "agent_up.sh")],
        env={**os.environ, "PATH": f"{sandbox / 'bin'}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert "AGENT_INTERNAL_TOKEN is unset" in result.stderr


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


def test_no_random_source_warns_and_does_not_abort(sandbox: Path) -> None:
    """start.sh runs under set -e; a failed openssl and od must not stop it."""
    _stub(sandbox, "openssl", "exit 1")
    _stub(sandbox, "od", "exit 1")
    env_file = sandbox / ".env"
    env_file.write_text('AGENT_INTERNAL_TOKEN=""\n')

    result = _ensure(sandbox)

    assert result.returncode == 0, result.stderr
    assert "could not generate AGENT_INTERNAL_TOKEN" in result.stderr
    assert "exported=\n" in result.stdout
    assert env_file.read_text() == 'AGENT_INTERNAL_TOKEN=""\n'


def test_keeps_the_file_mode(sandbox: Path) -> None:
    env_file = sandbox / ".env"
    env_file.write_text('AGENT_INTERNAL_TOKEN=""\n')
    env_file.chmod(0o640)

    assert _ensure(sandbox).returncode == 0
    assert env_file.stat().st_mode & 0o777 == 0o640
    assert not list(sandbox.glob(".env.tmp*"))


def test_a_failed_write_keeps_the_owner_only_copy(sandbox: Path) -> None:
    env_file = sandbox / ".env"
    env_file.write_text('AGENT_INTERNAL_TOKEN=""\n')
    env_file.chmod(0o400)

    result = _ensure(sandbox)

    assert result.returncode == 0, result.stderr
    assert "could not rewrite .env" in result.stderr
    (copy,) = sandbox.glob(".env.tmp*")
    assert copy.stat().st_mode & 0o777 == 0o600
    assert HEX_TOKEN.fullmatch(copy.read_text().strip())
    assert env_file.read_text() == 'AGENT_INTERNAL_TOKEN=""\n'


def test_a_new_env_is_owner_only(sandbox: Path) -> None:
    shutil.copy(REPO / "env.example", sandbox / "env.example")

    assert _bash(sandbox, "load_env").returncode == 0
    assert (sandbox / ".env").stat().st_mode & 0o777 == 0o600


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
    call = start.index("\nensure_agent_token\n")
    assert call < start.index("\nstart_autostart_services\n")
    assert call < start.index("    start_agent_layer\n")
