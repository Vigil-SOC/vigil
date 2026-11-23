#!/usr/bin/env python3
# The file core/upgrade/versions/HEAD names the only Alembic head. Two revision
# PRs written against the same head both change that one line, so the second
# conflicts and cannot merge until it is rebased.

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT / "core" / "upgrade" / "versions" / "HEAD"
sys.path.insert(0, str(ROOT))

from core.upgrade.command import script_directory  # noqa: E402

REMEDY = (
    "Rebase onto main and either point your revision's down_revision at the "
    "new head, or add a merge revision, then write the new head into "
    "core/upgrade/versions/HEAD."
)


def read_pin() -> str | None:
    """The revision id on the pin's single line, or None when it is not one."""
    if not PIN.is_file():
        return None
    raw = PIN.read_text(encoding="utf-8")
    if raw.endswith("\n"):
        raw = raw[:-1]
    if raw == "" or "\n" in raw or raw != raw.strip():
        return None
    return raw


def main() -> int:
    heads = list(script_directory().get_heads())
    pinned = read_pin()
    if len(heads) == 1 and pinned == heads[0]:
        print(f"schema head: {heads[0]}")
        return 0

    if len(heads) != 1:
        print(f"schema head: this image has {len(heads)} Alembic heads: {heads}")
    else:
        print(f"schema head: the only Alembic head is {heads[0]!r}")
    if not PIN.is_file():
        print("core/upgrade/versions/HEAD is missing")
    elif pinned is None:
        print("core/upgrade/versions/HEAD is not exactly one revision id")
    else:
        print(f"core/upgrade/versions/HEAD is {pinned!r}")
    print(REMEDY)
    return 1


if __name__ == "__main__":
    sys.exit(main())
