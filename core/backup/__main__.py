"""``python -m core.backup`` create, restore, the schedule loop, and the current point."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from core.backup.create import (
    BackupError,
    BackupSkipped,
    _priority_prefix,
    create_snapshot,
)
from core.backup.preupgrade import (
    DEFAULT_UPGRADE_DIR,
    KIND,
    RESTORE_POINT_NAME,
    pre_upgrade_snapshot,
)
from core.backup.restore import _load_manifest, _restic, restore_snapshot
from core.backup.schedule import (
    _applied_times,
    _configure_logging,
    _tagged,
    run_forever,
)
from core.config import get_settings


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
        help="snapshot when this image would migrate or the recorded version changed",
    )
    pre.add_argument("--bifrost-data", help="Bifrost data directory to include")
    sub.add_parser(
        "current-point",
        help="print the newest pre-upgrade snapshot in the upgrade directory as JSON",
    )
    return parser


def current_point() -> dict:
    """The pre-upgrade snapshot to restore, or {} when the no-setup repo has none.

    After the last applied upgrade that is the first point taken since: a
    retry after a failed upgrade snapshots the half-migrated database again,
    and that later point is not the way back. With none since, it is the
    newest, the point the last applied upgrade took. ``taken_by`` is the
    release that took it; ``version`` is the release it returns to.
    """
    raw = (get_settings().vigil_upgrade_dir or "").strip()
    upgrade_dir = Path(raw) if raw else DEFAULT_UPGRADE_DIR
    repo, passphrase = upgrade_dir / "repo", upgrade_dir / "passphrase"
    if not passphrase.is_file() or not (repo / "config").is_file():
        return {}
    snaps = _tagged(str(repo), passphrase, None, KIND)
    if not snaps:
        return {}
    applied = max(_applied_times(upgrade_dir), default=None)
    since = [s for s in snaps if applied is None or s.taken_at > applied]
    if since:
        point = min(since, key=lambda snap: snap.taken_at)
    else:
        point = max(snaps, key=lambda snap: snap.taken_at)
    manifest, _dump, snapshot_id = _load_manifest(
        _restic(str(repo), True), passphrase, point.snapshot_id, _priority_prefix()
    )
    return {
        "snapshot": snapshot_id,
        "taken_by": manifest.get("taken_by"),
        "version": manifest.get("version"),
        "created_at": manifest.get("created_at"),
    }


def _publish_restore_point(snapshot_id: str | None) -> None:
    """Leave the id where the upgrade one-shot reads it, or clear a stale one.

    stdout stays empty: the id is only logged. A restart or an acknowledgement
    takes no snapshot, and a previous id must not be passed as this run's.
    """
    raw = (get_settings().vigil_upgrade_dir or "").strip()
    if not raw:
        if snapshot_id:
            raise BackupError(
                "pre-upgrade snapshot taken but VIGIL_UPGRADE_DIR is unset"
            )
        return
    path = Path(raw) / RESTORE_POINT_NAME
    if not snapshot_id:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(snapshot_id + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


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
            _publish_restore_point(pre_upgrade_snapshot(bifrost_data=args.bifrost_data))
            return 0
        if args.command == "current-point":
            print(json.dumps(current_point()))
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
