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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping

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
    # (restic env var name, secrets.enc key) pairs.
    env: tuple[tuple[str, str], ...] = ()


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
    env = _parse_env(item.get("env"), name)
    if env is None:
        return None
    return Destination(
        name=name.strip(),
        repo=repo.strip(),
        passphrase_secret=secret.strip(),
        interval_hours=interval,
        keep_last=keep,
        default=item.get("default") is True,
        must_be_mount=item.get("must_be_mount") is True,
        env=env,
    )


def _parse_env(value: object, name: str) -> tuple[tuple[str, str], ...] | None:
    if value is None:
        return ()
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()
        for k, v in value.items()
    ):
        logger.error("destination %s: env must be an object of non-empty strings", name)
        return None
    pairs = tuple((k.strip(), v.strip()) for k, v in value.items())
    reserved = [k for k, _ in pairs if k.upper().startswith("RESTIC_PASSWORD")]
    if reserved:
        logger.error(
            "destination %s: env must not set %s; use passphrase_secret",
            name,
            ", ".join(reserved),
        )
        return None
    return pairs


def _count(value: object, *, minimum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < minimum:
        return None
    return value


@contextmanager
def prepared_destination(
    dest: Destination,
) -> Iterator[tuple[Path, dict[str, str]]]:
    """Check the mount, resolve secrets, then yield the passphrase file and restic env."""
    if dest.must_be_mount and not os.path.ismount(dest.repo):
        raise BackupError(f"{dest.repo} is not a mount")
    passphrase = get_secret(dest.passphrase_secret)
    if not passphrase:
        raise BackupError(f"passphrase secret {dest.passphrase_secret} is not set")
    extra: dict[str, str] = {}
    for var, key in dest.env:
        value = get_secret(key)
        if not value:
            raise BackupError(f"secret {key} is not set")
        extra[var] = value
    fd, raw_path = tempfile.mkstemp(prefix="vigil-backup-pass-")
    path = Path(raw_path)
    try:
        os.chmod(path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(passphrase)
        yield path, extra
    except BackupSkipped:
        raise
    except BackupError as exc:
        # restic stderr can echo credentials; scrub before it is logged.
        message = _scrub(str(exc), extra.values())
        if message == str(exc):
            raise
        raise BackupError(message) from None
    finally:
        path.unlink(missing_ok=True)


def _run_destination(dest: Destination, *, bifrost_data: str | None) -> None:
    with prepared_destination(dest) as (path, extra):
        if not _is_due(dest, path, extra):
            logger.debug("destination %s is not due", dest.name)
            return
        snapshot_id = create_snapshot(
            repo=dest.repo,
            passphrase_file=str(path),
            bifrost_data=bifrost_data,
            kind="scheduled",
            tags=("scheduled",),
            extra_env=extra,
        )
        _forget(dest, path, extra)
        _write_status(dest.name, snapshot_id)


def _scrub(message: str, secrets: Iterable[str]) -> str:
    for value in sorted(secrets, key=len, reverse=True):
        message = message.replace(value, "***")
    return message


def _is_due(
    dest: Destination, passphrase: Path, extra: Mapping[str, str] | None = None
) -> bool:
    latest = _latest_snapshot_at(dest.repo, passphrase, extra)
    if latest is None:
        return True
    return datetime.now(timezone.utc) - latest >= timedelta(hours=dest.interval_hours)


def _latest_snapshot_at(
    repo: str, passphrase: Path, extra: Mapping[str, str] | None = None
) -> datetime | None:
    local = _local_repo(repo)
    if local is not None and not (local / "config").is_file():
        return None
    proc = _run(
        _priority_prefix() + ["restic", "-r", repo, "snapshots", "--json"],
        env=_restic_env(passphrase, extra),
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


def _forget(
    dest: Destination, passphrase: Path, extra: Mapping[str, str] | None = None
) -> None:
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
        env=_restic_env(passphrase, extra),
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
