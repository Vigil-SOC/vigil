"""Loop over ``vigil_path("backups.json")`` and snapshot destinations that are due.

One pass that finds nothing, or a missing file, returns. The process stays
up; exiting would restart the container. Due is the newest snapshot in that
destination's repository, not ``backup_status.json``, which stays one object.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.backup.create import (
    SKIPPED_MESSAGE,
    BackupError,
    BackupSkipped,
    _local_repo,
    _output,
    _priority_prefix,
    _restic_env,
    _run,
    create_snapshot,
)
from core.backup.status import STATUS_FILENAME
from core.config import vigil_path
from core.secrets import get_secret

logger = logging.getLogger(__name__)

# Due follows a snapshot tagged as a real backup. create deletes a snapshot
# whose verification failed, so an untagged id does not count as success.
_SUCCESS_TAGS = frozenset({"manual", "scheduled", "pre-upgrade", "safety"})

DESTINATIONS_FILENAME = "backups.json"
POLL_SECONDS = 60
_DEFAULT_INTERVAL_HOURS = 24
_DEFAULT_KEEP_LAST = 14


@dataclass(frozen=True)
class Destination:
    name: str
    repo: str
    passphrase_secret: str
    interval_hours: int
    keep_last: int
    default: bool
    must_be_mount: bool


def run_forever(*, bifrost_data: str | None) -> None:
    _configure_logging()
    while True:
        try:
            run_due(bifrost_data=bifrost_data)
        except Exception:
            logger.exception("backup schedule pass failed")
        time.sleep(POLL_SECONDS)


def run_due(*, bifrost_data: str | None) -> None:
    for dest in _load_destinations():
        try:
            _run_destination(dest, bifrost_data=bifrost_data)
        except BackupSkipped:
            logger.warning("%s", SKIPPED_MESSAGE)
        except BackupError as exc:
            logger.error("destination %s: %s", dest.name, exc)
        except Exception:
            logger.exception("destination %s failed", dest.name)


def _configure_logging() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def _load_destinations() -> list[Destination]:
    path = vigil_path(DESTINATIONS_FILENAME)
    if not path.is_file():
        logger.info("%s is missing; waiting", DESTINATIONS_FILENAME)
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.error("could not read %s: %s", DESTINATIONS_FILENAME, exc)
        return []
    if not raw:
        logger.info("%s is empty; waiting", DESTINATIONS_FILENAME)
        return []
    if not isinstance(raw, list):
        logger.error("%s must be a list of destinations", DESTINATIONS_FILENAME)
        return []
    found: list[Destination] = []
    for item in raw:
        dest = _parse_destination(item)
        if dest is not None:
            found.append(dest)
    return found


def _parse_destination(item: object) -> Destination | None:
    if not isinstance(item, dict):
        logger.error("skipping a destination entry that is not an object")
        return None
    name = item.get("name")
    repo = item.get("repo")
    secret = item.get("passphrase_secret")
    if (
        not isinstance(name, str)
        or not isinstance(repo, str)
        or not isinstance(secret, str)
        or not name.strip()
        or not repo.strip()
        or not secret.strip()
    ):
        logger.error(
            "skipping a destination that lacks name, repo, or passphrase_secret"
        )
        return None
    interval = _count(item.get("interval_hours", _DEFAULT_INTERVAL_HOURS), minimum=1)
    keep = _count(item.get("keep_last", _DEFAULT_KEEP_LAST), minimum=1)
    if interval is None or keep is None:
        logger.error(
            "destination %s: interval_hours and keep_last must be integers >= 1",
            name,
        )
        return None
    return Destination(
        name=name.strip(),
        repo=repo.strip(),
        passphrase_secret=secret.strip(),
        interval_hours=interval,
        keep_last=keep,
        default=item.get("default") is True,
        must_be_mount=item.get("must_be_mount") is True,
    )


def _count(value: object, *, minimum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < minimum:
        return None
    return value


def _run_destination(dest: Destination, *, bifrost_data: str | None) -> None:
    if dest.must_be_mount and not os.path.ismount(dest.repo):
        raise BackupError(f"{dest.repo} is not a mount")
    passphrase = get_secret(dest.passphrase_secret)
    if not passphrase:
        raise BackupError(f"passphrase secret {dest.passphrase_secret} is not set")
    fd, raw_path = tempfile.mkstemp(prefix="vigil-backup-pass-")
    path = Path(raw_path)
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(passphrase)
        if not _is_due(dest, path):
            logger.debug("destination %s is not due", dest.name)
            return
        snapshot_id = create_snapshot(
            repo=dest.repo,
            passphrase_file=str(path),
            bifrost_data=bifrost_data,
            kind="scheduled",
            tags=("scheduled",),
        )
        _forget(dest, path)
        _write_status(dest.name, snapshot_id)
    finally:
        path.unlink(missing_ok=True)


def _is_due(dest: Destination, passphrase: Path) -> bool:
    latest = _latest_snapshot_at(dest.repo, passphrase)
    if latest is None:
        return True
    return datetime.now(timezone.utc) - latest >= timedelta(hours=dest.interval_hours)


def _latest_snapshot_at(repo: str, passphrase: Path) -> datetime | None:
    local = _local_repo(repo)
    if local is not None and not (local / "config").is_file():
        return None
    proc = _run(
        _priority_prefix() + ["restic", "-r", repo, "snapshots", "--json"],
        env=_restic_env(passphrase),
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    latest: datetime | None = None
    for snap in payload:
        if not isinstance(snap, dict):
            continue
        tags = snap.get("tags") or []
        if not isinstance(tags, list) or not _SUCCESS_TAGS.intersection(tags):
            continue
        parsed = _parse_time(snap.get("time"))
        if parsed is not None and (latest is None or parsed > latest):
            latest = parsed
    return latest


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "." in text:
        head, frac = text.split(".", 1)
        sign = next((i for i, char in enumerate(frac) if char in "+-"), len(frac))
        text = f"{head}.{frac[:sign][:6]}{frac[sign:]}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _forget(dest: Destination, passphrase: Path) -> None:
    # ``s3:`` / ``gs:`` / ``azure:`` keep whatever the bucket's rules keep.
    if _local_repo(dest.repo) is None:
        return
    # Default grouping is host,paths. Staging paths change every run, so
    # that would keep every snapshot. ``--tag`` twice is OR; one
    # ``--tag manual,scheduled`` would require both and skip these.
    proc = _run(
        _priority_prefix()
        + [
            "restic",
            "-r",
            dest.repo,
            "forget",
            "--keep-last",
            str(dest.keep_last),
            "--prune",
            "--tag",
            "manual",
            "--tag",
            "scheduled",
            "--group-by",
            "",
        ],
        env=_restic_env(passphrase),
        check=False,
    )
    if proc.returncode != 0:
        raise BackupError(f"restic forget failed: {_output(proc)}")


def _write_status(destination: str, snapshot_id: str) -> None:
    path = vigil_path(STATUS_FILENAME, write=True)
    payload = {
        "destination": destination,
        "last_success_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_id": snapshot_id,
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
