"""The desktop app's version tracks the release, or it pulls stale images.

Issue #1110: release-please bumped VERSION and clients/web but never
clients/desktop, so the app kept passing VIGIL_VERSION=0.3.0 to docker compose
and pulled 0.3.0 backend/agent images from a 0.5.0 repo.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
DESKTOP = REPO / "clients" / "desktop"
SUPPORT = REPO / "scripts" / "vigil-support"
BUILD_TARBALL = REPO / "scripts" / "build-support-tarball.sh"

DESKTOP_VERSION_FIELDS = (
    ("clients/desktop/package.json", "$.version"),
    ("clients/desktop/package-lock.json", "$.version"),
    ("clients/desktop/package-lock.json", "$.packages[''].version"),
)


def _load(name: str) -> dict:
    return json.loads((DESKTOP / name).read_text(encoding="utf-8"))


def test_desktop_versions_match_release_version() -> None:
    release = (REPO / "VERSION").read_text(encoding="utf-8").strip()
    lock = _load("package-lock.json")
    assert _load("package.json")["version"] == release
    assert lock["version"] == release
    assert lock["packages"][""]["version"] == release


def test_release_please_bumps_desktop_versions() -> None:
    # Without these entries the next release reintroduces the drift.
    config = json.loads(
        (REPO / ".github" / "release-please-config.json").read_text(encoding="utf-8")
    )
    listed = {
        (f["path"], f["jsonpath"])
        for f in config["packages"]["."]["extra-files"]
        if f.get("type") == "json"
    }
    for field in DESKTOP_VERSION_FIELDS:
        assert field in listed, f"release-please does not bump {field}"


def test_support_version_tracks_release() -> None:
    # Committed and bumped by release-please; the tarball restamps it anyway.
    release = (REPO / "VERSION").read_text(encoding="utf-8").strip()
    first = (SUPPORT / "VERSION").read_text(encoding="utf-8").split()[0]
    assert first == release


def test_desktop_ships_support_script() -> None:
    builder = (DESKTOP / "electron-builder.yml").read_text(encoding="utf-8")
    assert "from: ../../scripts/vigil-support" in builder
    assert "to: vigil-support" in builder


def test_release_attaches_support_tarball() -> None:
    wf = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    job = wf[wf.index("  update-release:") :]
    # Built by the committed script from the tag's version, then attached with its checksum.
    assert (
        'scripts/build-support-tarball.sh "${{ needs.version.outputs.version }}"' in job
    )
    assert "vigil-support-${{ needs.version.outputs.version }}.tar.gz\n" in job
    assert "vigil-support-${{ needs.version.outputs.version }}.tar.gz.sha256" in job
    assert "actions/checkout" in job


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh script")
def test_support_tarball_is_stamped_runnable_and_verifiable(tmp_path: Path) -> None:
    subprocess.run(["sh", str(BUILD_TARBALL), "9.8.7", str(tmp_path)], check=True)
    name = "vigil-support-9.8.7.tar.gz"
    assert (tmp_path / f"{name}.sha256").read_text().split()[1] == name

    for check in (["sha256sum", "-c"], ["shasum", "-a", "256", "-c"]):
        if shutil.which(check[0]):
            subprocess.run([*check, f"{name}.sha256"], cwd=tmp_path, check=True)

    with tarfile.open(tmp_path / name) as tar:
        tar.extractall(tmp_path / "x")
    root = tmp_path / "x" / "vigil-support"
    assert (root / "VERSION").read_text() == "9.8.7\n"
    out = subprocess.run(
        ["sh", str(root / "vigil-support.sh"), "--help"], capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
