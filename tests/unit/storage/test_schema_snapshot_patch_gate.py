"""A patch bump of VERSION fails when the column snapshot changed since the last tag.

The gate compares the PR's VERSION to the previous ``v*`` tag. Same
major.minor plus a different snapshot fails, and the message names
``Release-As``. A minor bump passes. A resolved tag with no snapshot file
passes (bootstrap). An unknown tag fails.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

pytestmark = pytest.mark.unit

from scripts.check_schema_snapshot_patch import (  # noqa: E402
    SNAPSHOT_PATH,
    gate_result,
    previous_tag,
    show_path,
)

SNAP_A = '{\n  "cases": {\n    "id": {\n      "data_type": "text",\n      "nullable": false\n    }\n  }\n}\n'
SNAP_B = '{\n  "cases": {\n    "id": {\n      "data_type": "uuid",\n      "nullable": false\n    }\n  }\n}\n'


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _commit(repo: Path, version: str, snapshot: str | None) -> None:
    (repo / "VERSION").write_text(version + "\n", encoding="utf-8")
    path = repo / SNAPSHOT_PATH
    if snapshot is None:
        if path.exists():
            path.unlink()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(snapshot, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", f"version {version}")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "gate@example.com")
    _git(tmp_path, "config", "user.name", "gate")
    _commit(tmp_path, "0.6.0", SNAP_A)
    _git(tmp_path, "tag", "v0.6.0")
    return tmp_path


def test_previous_tag_is_the_greatest_strictly_older_release():
    assert (
        previous_tag(["v0.5.0", "v0.6.0", "v0.6.1-rc1", "nope"], (0, 6, 1)) == "v0.6.0"
    )
    assert previous_tag(["v0.6.0"], (0, 7, 0)) == "v0.6.0"
    assert previous_tag([], (0, 6, 1)) is None


def test_patch_release_with_a_changed_snapshot_names_release_as(repo: Path):
    base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    _commit(repo, "0.6.1", SNAP_B)
    code, message = gate_result(repo, base)
    assert code == 1
    assert "Release-As: 0.7.0" in message


def test_minor_release_may_change_the_snapshot(repo: Path):
    base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    _commit(repo, "0.7.0", SNAP_B)
    code, message = gate_result(repo, base)
    assert code == 0
    assert "changes major.minor" in message


def test_patch_release_with_the_same_snapshot_passes(repo: Path):
    base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    _commit(repo, "0.6.1", SNAP_A)
    code, _message = gate_result(repo, base)
    assert code == 0


def test_version_left_alone_stays_green_when_the_snapshot_changes(repo: Path):
    base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    _commit(repo, "0.6.0", SNAP_B)
    code, message = gate_result(repo, base)
    assert code == 0
    assert "VERSION unchanged" in message


def test_resolved_tag_without_a_snapshot_file_bootstraps(tmp_path: Path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "gate@example.com")
    _git(tmp_path, "config", "user.name", "gate")
    _commit(tmp_path, "0.6.0", None)
    _git(tmp_path, "tag", "v0.6.0")
    base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()
    _commit(tmp_path, "0.6.1", SNAP_A)
    code, message = gate_result(tmp_path, base)
    assert code == 0
    assert "no column snapshot" in message


def test_unresolved_tag_fails(repo: Path):
    base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    _git(repo, "tag", "-d", "v0.6.0")
    _commit(repo, "0.6.1", SNAP_B)
    code, message = gate_result(repo, base)
    assert code == 1
    assert "could not resolve a previous release tag" in message


def test_show_path_distinguishes_a_missing_file_from_an_unknown_tag(repo: Path):
    assert show_path(repo, "v0.6.0:VERSION").strip() == "0.6.0"
    assert show_path(repo, f"v0.6.0:{SNAPSHOT_PATH}") == SNAP_A
    assert show_path(repo, "v0.6.0:not/a/file") is None
    with pytest.raises(LookupError):
        show_path(repo, "v9.9.9:VERSION")
