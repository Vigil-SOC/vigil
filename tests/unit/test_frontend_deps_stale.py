"""start.sh reinstalls web deps when the lockfile is newer than the install (#3905)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

LIB = Path(__file__).resolve().parents[2] / "scripts" / "lib.sh"


def _stale(web: Path) -> bool:
    r = subprocess.run(
        ["bash", "-c", f'source "{LIB}"; frontend_deps_stale "{web}"'],
        capture_output=True,
        text=True,
    )
    assert r.returncode in (0, 1), r.stderr
    return r.returncode == 0


def _touch(path: Path, mtime: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    os.utime(path, (mtime, mtime))


def test_missing_node_modules_is_stale(tmp_path):
    _touch(tmp_path / "package-lock.json", 1000)
    assert _stale(tmp_path)


def test_lockfile_newer_than_install_is_stale(tmp_path):
    _touch(tmp_path / "node_modules" / ".package-lock.json", 1000)
    _touch(tmp_path / "package-lock.json", 2000)
    assert _stale(tmp_path)


def test_install_newer_than_lockfile_is_current(tmp_path):
    _touch(tmp_path / "package-lock.json", 1000)
    _touch(tmp_path / "node_modules" / ".package-lock.json", 2000)
    assert not _stale(tmp_path)


def test_no_lockfile_with_node_modules_is_current(tmp_path):
    (tmp_path / "node_modules").mkdir()
    assert not _stale(tmp_path)


def test_install_without_hidden_lockfile_is_stale(tmp_path):
    _touch(tmp_path / "package-lock.json", 1000)
    (tmp_path / "node_modules").mkdir()
    assert _stale(tmp_path)
