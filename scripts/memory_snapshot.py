#!/usr/bin/env python3
"""Create, list and drop frozen copies of episodic memory (#736).

An eval that reads live memory cannot tell you what it measured: the Distil
polls, so the corpus grows between two runs of the same hunts and the score
moves without the code changing. A snapshot is a schema holding a copy of the
episodic tables; an eval process reads one by putting it on its search_path.

Why a snapshot rather than an as-of filter, and why PGOPTIONS rather than the
DSN, is written down once in core/memory/snapshot.py.

Usage:
    python scripts/memory_snapshot.py create              # named for today
    python scripts/memory_snapshot.py create 2026_09_01
    python scripts/memory_snapshot.py create control --empty
    python scripts/memory_snapshot.py list
    python scripts/memory_snapshot.py drop 2026_09_01

Then point an eval process at one:

    PGOPTIONS='-c search_path=episodic_snap_2026_09_01,public' <the eval>
"""

import argparse
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from core.memory.snapshot import (  # noqa: E402
    SnapshotError,
    create_snapshot,
    drop_snapshot,
    list_snapshots,
)
from core.storage.connection import get_db_manager  # noqa: E402


def _create(args) -> int:
    snapshot = create_snapshot(args.name, empty=args.empty)
    total = sum(snapshot.rows.values())
    print(f"{snapshot.name}  schema={snapshot.schema}  rows={total}")
    for table, count in snapshot.rows.items():
        print(f"  {table:<26} {count}")
    print(f"\nRead it with:\n  PGOPTIONS='{snapshot.pgoptions}' <the eval process>")
    return 0


def _list(_args) -> int:
    found = list_snapshots()
    if not found:
        print("no snapshots")
        return 0
    for snapshot in found:
        stamp = snapshot.created_at.isoformat() if snapshot.created_at else "unknown"
        total = sum(snapshot.rows.values())
        label = "empty" if snapshot.is_empty else f"{total} rows"
        print(f"{snapshot.name:<24} {stamp:<32} {label}")
    return 0


def _drop(args) -> int:
    # Bounded because anything still reading the snapshot holds its locks and
    # DROP waits rather than failing: a person at a terminal should be told
    # something is reading it, not left watching a cursor.
    drop_snapshot(args.name, lock_timeout="10s")
    print(f"dropped {args.name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    create_cmd = commands.add_parser(
        "create", help="copy the live tier into a new schema"
    )
    create_cmd.add_argument("name", nargs="?", help="defaults to today's date")
    create_cmd.add_argument(
        "--empty",
        action="store_true",
        help="copy no rows: the control an A/B measures against",
    )
    create_cmd.set_defaults(run=_create)

    commands.add_parser("list", help="every snapshot in the database").set_defaults(
        run=_list
    )

    drop_cmd = commands.add_parser(
        "drop", help="delete a snapshot and everything in it"
    )
    drop_cmd.add_argument("name")
    drop_cmd.set_defaults(run=_drop)

    args = parser.parse_args()
    get_db_manager().initialize()
    try:
        return args.run(args)
    except SnapshotError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
