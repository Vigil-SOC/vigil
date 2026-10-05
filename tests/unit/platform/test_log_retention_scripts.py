"""Log retention in scripts/lib.sh: rotate_log and save_container_logs.

Drives the shell functions from a temp copy of lib.sh (so REPO_ROOT, and the
logs/ directory, are temporary) with a stub `docker` first on PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]

STUB_DOCKER = """#!/bin/bash
case "$1" in
  compose) exit 0 ;;
  ps) [ "${STUB_DOCKER:-ok}" = fail ] && exit 1; echo abc123; exit 0 ;;
  logs)
    case "${STUB_DOCKER:-ok}" in
      fail) echo "daemon error" >&2; exit 1 ;;
      hang) sleep 30 ;;
    esac
    echo "$*" > "$ROOT_FOR_STUB/last_logs_args"
    echo "2026-01-01T00:00:00Z line from $STUB_RUN" ;;
esac
"""


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    shutil.copy(REPO / "scripts" / "lib.sh", tmp_path / "scripts" / "lib.sh")
    (tmp_path / "bin").mkdir()
    docker = tmp_path / "bin" / "docker"
    docker.write_text(STUB_DOCKER)
    docker.chmod(0o755)
    return tmp_path


def _bash(root: Path, body: str, **env: str) -> subprocess.CompletedProcess[str]:
    full_env = {
        **os.environ,
        "PATH": f"{root / 'bin'}:{os.environ['PATH']}",
        "ROOT_FOR_STUB": str(root),
        **env,
    }
    return subprocess.run(
        ["bash", "-c", f'source "{root}/scripts/lib.sh"\n{body}'],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_rotate_log_keeps_five_runs_newest_first(sandbox: Path) -> None:
    logs = sandbox / "logs"
    logs.mkdir()
    for run in range(1, 8):
        (logs / "backend.log").write_text(f"run {run}")
        assert _bash(sandbox, f'rotate_log "{logs}/backend.log"').returncode == 0
    (logs / "backend.log").write_text("run 8")
    assert [
        (logs / name).read_text()
        for name in ("backend.log", *(f"backend.log.{i}" for i in range(1, 5)))
    ] == ["run 8", "run 7", "run 6", "run 5", "run 4"]
    assert not (logs / "backend.log.5").exists()


def test_rotate_log_skips_missing_and_empty(sandbox: Path) -> None:
    logs = sandbox / "logs"
    logs.mkdir()
    (logs / "empty.log").write_text("")
    done = _bash(
        sandbox, f'rotate_log "{logs}/missing.log"; rotate_log "{logs}/empty.log"'
    )
    assert done.returncode == 0
    assert sorted(p.name for p in logs.iterdir()) == ["empty.log"]


def test_save_container_logs_names_and_keeps_five(sandbox: Path) -> None:
    containers = sandbox / "logs" / "containers"
    containers.mkdir(parents=True)
    # Seven old saves of this container, plus another container's that must survive.
    for i in range(7):
        (containers / f"deeptempo-postgres-2025010{i + 1}T000000Z.log").write_text(
            "old"
        )
    (containers / "deeptempo-postgres-test-20250101T000000Z.log").write_text("other")

    done = _bash(sandbox, "save_container_logs deeptempo-postgres", STUB_RUN="now")
    assert done.returncode == 0

    saved = sorted(containers.glob("deeptempo-postgres-[0-9]*.log"))
    assert len(saved) == 5
    newest = saved[-1]
    assert "line from now" in newest.read_text()
    assert (containers / "deeptempo-postgres-test-20250101T000000Z.log").exists()
    assert "--timestamps" in (sandbox / "last_logs_args").read_text()
    assert not list(containers.glob("*.tmp"))


def test_save_container_logs_never_fails_startup(sandbox: Path) -> None:
    done = _bash(
        sandbox,
        "set -e; save_container_logs x; save_project_container_logs; echo survived",
        STUB_DOCKER="fail",
    )
    assert done.returncode == 0
    assert "survived" in done.stdout
    assert not list((sandbox / "logs").rglob("*.log"))


def test_save_container_logs_is_bounded_when_docker_hangs(sandbox: Path) -> None:
    started = time.monotonic()
    done = _bash(
        sandbox,
        "save_container_logs x; echo survived",
        STUB_DOCKER="hang",
        VIGIL_LOG_SAVE_TIMEOUT="1",
    )
    assert time.monotonic() - started < 15
    assert done.returncode == 0
    assert "survived" in done.stdout
    assert not list((sandbox / "logs").rglob("*.log"))
