"""Every Dependabot entry points at a real manifest, and every lockfile is watched.

Issue #2584: four entries named folders that had moved, so Dependabot skipped
them silently and npm and Docker dependencies got no update PRs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
CONFIG = REPO / ".github" / "dependabot.yml"

# Globs each ecosystem reads in the named directory.
MANIFESTS = {
    "npm": ("package.json",),
    "docker": ("Dockerfile*", "Containerfile*"),
    "uv": ("pyproject.toml", "uv.lock"),
    "pip": ("requirements*.txt", "pyproject.toml"),
    "github-actions": (".github/workflows/*.yml", ".github/workflows/*.yaml"),
}


def _entries() -> list[tuple[str, str]]:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    return [
        (u["package-ecosystem"], u["directory"].strip("/")) for u in config["updates"]
    ]


def _tracked(*patterns: str) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--", *patterns],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [Path(line) for line in out.splitlines()]


@pytest.mark.parametrize(("ecosystem", "directory"), _entries())
def test_directory_holds_a_manifest_its_ecosystem_reads(
    ecosystem: str, directory: str
) -> None:
    root = REPO / directory
    assert root.is_dir(), f"{ecosystem} watches /{directory}, which does not exist"
    assert any(any(root.glob(g)) for g in MANIFESTS[ecosystem]), (
        f"/{directory} has no {ecosystem} manifest ({', '.join(MANIFESTS[ecosystem])})"
    )


@pytest.mark.parametrize(
    ("ecosystem", "patterns"),
    [
        ("npm", ("package-lock.json", "**/package-lock.json")),
        ("uv", ("uv.lock", "**/uv.lock")),
        ("docker", ("infra/docker/Dockerfile*",)),
    ],
)
def test_every_tracked_manifest_is_watched(
    ecosystem: str, patterns: tuple[str, ...]
) -> None:
    watched = {d for e, d in _entries() if e == ecosystem}
    missing = [
        str(p)
        for p in _tracked(*patterns)
        if p.parent.as_posix().strip(".") not in watched
    ]
    assert not missing, f"no {ecosystem} Dependabot entry covers: {missing}"
