"""The desktop app's version tracks the release, or it pulls stale images.

Issue #1110: release-please bumped VERSION and clients/web but never
clients/desktop, so the app kept passing VIGIL_VERSION=0.3.0 to docker compose
and pulled 0.3.0 backend/agent images from a 0.5.0 repo.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
DESKTOP = REPO / "clients" / "desktop"

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
