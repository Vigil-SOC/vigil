"""``python -m core.backup`` create, restore, and the schedule loop."""

from __future__ import annotations

import argparse
import sys

from core.backup.create import BackupError, BackupSkipped, create_snapshot
from core.backup.preupgrade import pre_upgrade_snapshot
from core.backup.restore import restore_snapshot
from core.backup.schedule import _configure_logging, run_forever


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
    restore = sub.add_parser("restore", help="stage, check, and swap in a snapshot")
    restore.add_argument("--repo", required=True, help="restic repository (local path)")
    restore.add_argument("--passphrase-file", required=True)
    restore.add_argument("--snapshot", default="latest")
    restore.add_argument(
        "--bifrost-data",
        help="Bifrost data directory to restore into; required when the snapshot has one",
    )
    restore.add_argument(
        "--test",
        action="store_true",
        help="run the checks, then drop the stage without renaming",
    )
    restore.add_argument(
        "--actor",
        default=None,
        help="recorded as changed_by on the restore audit row (default: the OS user)",
    )
    run = sub.add_parser(
        "run", help="snapshot destinations in backups.json that are due"
    )
    run.add_argument(
        "--bifrost-data",
        help="Bifrost data directory passed to each scheduled snapshot",
    )
    pre = sub.add_parser(
        "pre-upgrade",
        help="snapshot the default destination if the database is a release behind",
    )
    pre.add_argument(
        "--target-version", help="release being installed (default: this code's)"
    )
    pre.add_argument("--bifrost-data", help="Bifrost data directory to include")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            print(
                create_snapshot(
                    repo=args.repo,
                    passphrase_file=args.passphrase_file,
                    bifrost_data=args.bifrost_data,
                )
            )
            return 0
        if args.command == "restore":
            print(
                restore_snapshot(
                    repo=args.repo,
                    passphrase_file=args.passphrase_file,
                    snapshot=args.snapshot,
                    test=args.test,
                    actor=args.actor,
                    bifrost_data=args.bifrost_data,
                )
            )
            return 0
        if args.command == "run":
            run_forever(bifrost_data=args.bifrost_data)
            return 0
        if args.command == "pre-upgrade":
            _configure_logging()
            pre_upgrade_snapshot(
                target_version=args.target_version, bifrost_data=args.bifrost_data
            )
            return 0
    except BackupSkipped as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except BackupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"unknown command {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
