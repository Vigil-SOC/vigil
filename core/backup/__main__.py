"""``python -m core.backup create``."""

from __future__ import annotations

import argparse
import sys

from core.backup.create import BackupError, BackupSkipped, create_snapshot


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m core.backup")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="write one verified snapshot")
    create.add_argument("--repo", required=True, help="restic repository (local path)")
    create.add_argument("--passphrase-file", required=True)
    create.add_argument(
        "--bifrost-data",
        help="Bifrost data directory; omit to leave that location out",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "create":
        print(f"unknown command {args.command}", file=sys.stderr)
        return 2
    try:
        snapshot_id = create_snapshot(
            repo=args.repo,
            passphrase_file=args.passphrase_file,
            bifrost_data=args.bifrost_data,
        )
    except BackupSkipped as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except BackupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(snapshot_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
