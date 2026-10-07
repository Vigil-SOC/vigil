#!/usr/bin/env python3
"""Fail a patch release whose column snapshot differs from the previous tag.

Runs on a PR that changes VERSION. The previous release is the greatest
``vMAJOR.MINOR.PATCH`` tag strictly older than the VERSION in the PR. When
that tag's major.minor matches, the snapshot at the tag and at HEAD must
match: a column change has to ship as the next minor (a ``Release-As:``
footer). A minor or major bump passes. A tag that resolves but has no
snapshot file yet passes too — that is the first release to carry one.
A tag that does not resolve fails; those two outcomes are not the same.

``SCHEMA_GATE_BASE`` is the PR base commit. Unset, this is not a release
PR and the check passes.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

SNAPSHOT_PATH = "core/storage/schema.snapshot.json"
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

# git show exits 128 for both. A path absent from the tree says either
# "does not exist in '<rev>'" or, when the file is in the worktree,
# "exists on disk, but not in '<rev>'". An unknown rev says
# "invalid object name".
_BAD_OBJECT = "invalid object name"


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION.fullmatch(text.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def previous_tag(names: list[str], new: tuple[int, int, int]) -> str | None:
    """Greatest release tag strictly older than ``new``."""
    found: list[tuple[tuple[int, int, int], str]] = []
    for name in names:
        match = _TAG.fullmatch(name)
        if match is None:
            continue
        version = tuple(int(part) for part in match.groups())
        if version < new:
            found.append((version, name))
    if not found:
        return None
    return max(found)[1]


def _path_missing(err: str) -> bool:
    return "does not exist in" in err or "exists on disk, but not in" in err


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # Drop credential helpers so a missing origin fails immediately instead of
    # waiting on a prompt. The gate only reads objects already fetched.
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "true",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
    }
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env=env,
    )


def _fetch_tags(repo: Path) -> None:
    # A shallow checkout has no tag refs. Fetching them is what makes
    # `git show v0.6.0:<snapshot>` able to resolve; failing the fetch
    # leaves the tag unresolved, which the check rejects.
    _git(repo, "fetch", "--force", "--tags", "origin")


def tag_names(repo: Path) -> list[str]:
    listed = _git(repo, "tag", "-l", "v*")
    names = [line for line in listed.stdout.splitlines() if line]
    if names:
        return names
    _fetch_tags(repo)
    listed = _git(repo, "tag", "-l", "v*")
    return [line for line in listed.stdout.splitlines() if line]


def show_path(repo: Path, spec: str) -> str | None:
    """File text at ``spec``, None when that path is absent from a real tree.

    Raises ``LookupError`` when the object itself does not resolve.
    """
    shown = _git(repo, "show", spec)
    if shown.returncode == 0:
        return shown.stdout
    err = shown.stderr
    if _path_missing(err):
        return None
    if _BAD_OBJECT in err:
        _fetch_tags(repo)
        shown = _git(repo, "show", spec)
        if shown.returncode == 0:
            return shown.stdout
        if _path_missing(shown.stderr):
            return None
    raise LookupError(spec)


def version_changed(repo: Path, base: str) -> bool:
    """True when the PR itself edits VERSION (three-dot diff against base)."""
    diff = _git(repo, "diff", "--quiet", f"{base}...HEAD", "--", "VERSION")
    if diff.returncode == 0:
        return False
    if diff.returncode == 1:
        return True
    raise LookupError(base)


def gate_result(repo: Path, base: str) -> tuple[int, str]:
    try:
        changed = version_changed(repo, base)
    except LookupError:
        return 1, f"schema snapshot patch gate: could not resolve base {base}"
    if not changed:
        return 0, "schema snapshot patch gate: VERSION unchanged"

    try:
        new_text = show_path(repo, "HEAD:VERSION")
    except LookupError:
        return 1, "schema snapshot patch gate: could not resolve HEAD:VERSION"
    if new_text is None:
        return 1, "schema snapshot patch gate: VERSION is missing at HEAD"
    new = parse_version(new_text)
    if new is None:
        return (
            1,
            f"schema snapshot patch gate: VERSION {new_text.strip()!r} is not major.minor.patch",
        )

    tag = previous_tag(tag_names(repo), new)
    if tag is None:
        return (
            1,
            "schema snapshot patch gate: could not resolve a previous release tag",
        )
    tag_version = parse_version(tag[1:])
    assert tag_version is not None
    if (new[0], new[1]) != (tag_version[0], tag_version[1]):
        return (
            0,
            "schema snapshot patch gate: "
            f"{tag_version[0]}.{tag_version[1]} -> {new[0]}.{new[1]} "
            "changes major.minor",
        )

    spec = f"{tag}:{SNAPSHOT_PATH}"
    try:
        previous = show_path(repo, spec)
    except LookupError:
        return (
            1,
            f"schema snapshot patch gate: could not resolve {tag} "
            "(a missing snapshot file on a resolved tag is allowed; "
            "an unknown tag is not)",
        )
    if previous is None:
        return 0, f"schema snapshot patch gate: {tag} has no column snapshot yet"

    try:
        current = show_path(repo, f"HEAD:{SNAPSHOT_PATH}")
    except LookupError:
        return 1, "schema snapshot patch gate: could not resolve HEAD"
    if current != previous:
        nxt = f"{new[0]}.{new[1] + 1}.0"
        return (
            1,
            "schema snapshot patch gate: this patch release changes "
            f"{SNAPSHOT_PATH} relative to {tag}. Release it as the next minor, "
            f"for example with a footer:\n    Release-As: {nxt}",
        )
    return 0, f"schema snapshot patch gate: snapshot matches {tag}"


def main() -> int:
    base = os.environ.get("SCHEMA_GATE_BASE", "").strip()
    if not base:
        print("schema snapshot patch gate: SCHEMA_GATE_BASE unset, skipping")
        return 0
    code, message = gate_result(Path.cwd(), base)
    print(message, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
