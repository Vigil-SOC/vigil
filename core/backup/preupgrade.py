"""``python -m core.backup pre-upgrade``: snapshot before a revision changes the schema.

Run by Compose and ``start.sh`` ahead of everything that provisions the schema.
A point is taken when ``python -m core.upgrade --check`` would not restart and
would not refuse: revisions are pending, or the last applied history ``to``
differs from this image. ``--check`` exits 1 for ``needs_restore_point`` while
still printing the report; that exit is the report, not a failed check.

The snapshot goes to the default destination in ``backups.json`` when that
file has one. Otherwise it goes to ``repo/`` under the upgrade directory, with
a passphrase file beside that directory. A configured destination keeps its
own passphrase. Not enough space for the no-setup repo, or a failed snapshot,
refuses the upgrade with that reason.
"""

from __future__ import annotations

import io
import json
import logging
import os
import secrets
from pathlib import Path

from core.backup.connection import backup_database_config
from core.backup.create import BackupError, _local_repo, create_snapshot
from core.backup.schedule import (
    DESTINATIONS_FILENAME,
    Destination,
    _load_destinations,
    forget_pre_upgrade,
    prepared_destination,
)
from core.config import get_settings, vigil_path
from core.storage.connection import read_schema_version
from core.upgrade.command import (
    APPLY,
    EXIT_OK,
    EXIT_REFUSED,
    NEEDS_RESTORE_POINT,
    REFUSE,
    RESTART,
    _previous_version,
)
from core.upgrade.command import run as upgrade_run
from core.version import __version__

logger = logging.getLogger(__name__)

KIND = "pre-upgrade"
# One line, the snapshot id ``python -m core.upgrade --restore-point`` reads.
# The upgrade one-shot and this command share the upgrade directory.
RESTORE_POINT_NAME = "restore-point"
# No stamped release and no applied history yet. Not this image's version.
UNSTAMPED_RELEASE = "0.0.0"
DEFAULT_UPGRADE_DIR = Path("/var/lib/vigil-upgrade")
_VERDICTS = (RESTART, APPLY, NEEDS_RESTORE_POINT, REFUSE)


def pre_upgrade_snapshot(*, bifrost_data: str | None = None) -> str | None:
    """The new snapshot id, or None when none is due. Raises BackupError on failure."""
    if _acknowledged():
        logger.info("upgrade of %s acknowledged; no pre-upgrade backup", __version__)
        return None
    try:
        return _snapshot(bifrost_data)
    except BackupError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BackupError(f"pre-upgrade backup failed: {exc}") from exc


def _acknowledged() -> bool:
    settings = get_settings()
    return settings.vigil_upgrade_ack_version == __version__ and bool(
        settings.vigil_upgrade_ack_reason.strip()
    )


def _snapshot(bifrost_data: str | None) -> str | None:
    dest = _configured_destination()
    upgrade_dir = _open_upgrade_dir(no_setup=dest is None)
    report = _upgrade_report(upgrade_dir)
    verdict = report["verdict"]
    take = _due(verdict)
    release = _database_release(upgrade_dir)
    if dest is None:
        return _snapshot_default(upgrade_dir, report, take, release, bifrost_data)
    return _snapshot_configured(dest, upgrade_dir, report, take, release, bifrost_data)


def _configured_destination() -> Destination | None:
    destinations = _load_destinations()
    if destinations:
        return next((item for item in destinations if item.default), destinations[0])
    path = vigil_path(DESTINATIONS_FILENAME)
    if path.is_file() and path.read_text(encoding="utf-8").strip() not in ("", "[]"):
        raise BackupError(f"{DESTINATIONS_FILENAME} has no usable destination")
    return None


def _open_upgrade_dir(*, no_setup: bool) -> Path | None:
    """Create the upgrade directory before the report so ``free_bytes`` is set."""
    raw = (get_settings().vigil_upgrade_dir or "").strip()
    if raw:
        path = Path(raw)
    elif no_setup:
        path = DEFAULT_UPGRADE_DIR
    else:
        return None
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"could not create {path}: {exc}") from exc
    return path


def _upgrade_report(upgrade_dir: Path | None) -> dict:
    out = io.StringIO()
    code = upgrade_run(
        backup_database_config().get_database_url(),
        check=True,
        upgrade_dir=upgrade_dir,
        ack_version="",
        ack_reason="",
        state_dir=vigil_path(),
        out=out,
    )
    try:
        report = json.loads(out.getvalue())
    except json.JSONDecodeError as exc:
        raise BackupError(f"upgrade check returned no report: {exc}") from exc
    verdict = report.get("verdict") if isinstance(report, dict) else None
    # Exit 1 is needs_restore_point and refuse alike; the verdict says which.
    if code not in (EXIT_OK, EXIT_REFUSED) or verdict not in _VERDICTS:
        reason = report.get("reason") if isinstance(report, dict) else None
        raise BackupError(reason or "upgrade check failed")
    return report


def _due(verdict: str) -> bool:
    if verdict in (RESTART, REFUSE):
        return False
    if verdict in (NEEDS_RESTORE_POINT, APPLY):
        return True
    raise BackupError(f"upgrade check returned {verdict}")


def _database_release(upgrade_dir: Path | None) -> str:
    """The stamp in the database, else the last applied history ``to``."""
    stamp = read_schema_version(backup_database_config())
    if stamp:
        return stamp
    previous = _previous_version(upgrade_dir)
    if isinstance(previous, str) and previous.strip():
        return previous
    return UNSTAMPED_RELEASE


def _short_disk(report: dict) -> str | None:
    """Refusal for the no-setup repo, whose files share the upgrade directory."""
    free = report.get("free_bytes")
    need = report.get("restore_bytes")
    if (
        isinstance(free, bool)
        or isinstance(need, bool)
        or not isinstance(free, int)
        or not isinstance(need, int)
    ):
        return "not enough disk for a restore point: free space is unknown"
    if free < need:
        return (
            "not enough disk for a restore point: "
            f"{free} bytes free, {need} bytes required"
        )
    return None


def _ensure_default_repo(upgrade_dir: Path) -> tuple[str, Path]:
    """``repo/`` plus a mode-0600 passphrase beside it, not inside it."""
    repo = upgrade_dir / "repo"
    passphrase = upgrade_dir / "passphrase"
    if not passphrase.exists():
        if (repo / "config").is_file():
            raise BackupError(
                f"restore point repository {repo} exists but {passphrase.name} does not"
            )
        passphrase.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(passphrase, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, secrets.token_urlsafe(32).encode("utf-8"))
        finally:
            os.close(fd)
    os.chmod(passphrase, 0o600)
    return str(repo), passphrase


def _take(
    repo: str,
    passphrase: Path,
    extra: dict[str, str] | None,
    release: str,
    bifrost_data: str | None,
) -> str:
    return create_snapshot(
        repo=repo,
        passphrase_file=str(passphrase),
        bifrost_data=bifrost_data,
        kind=KIND,
        tags=(KIND,),
        extra_env=extra,
        version=release,
        taken_by=__version__,
    )


def _log_skip(report: dict) -> None:
    logger.info(
        "upgrade verdict %s (%s); no pre-upgrade backup",
        report.get("verdict"),
        report.get("reason"),
    )


def _snapshot_default(
    upgrade_dir: Path | None,
    report: dict,
    take: bool,
    release: str,
    bifrost_data: str | None,
) -> str | None:
    if upgrade_dir is None:
        raise BackupError("no upgrade directory for a restore point")
    snapshot_id: str | None = None
    repo = upgrade_dir / "repo"
    passphrase = upgrade_dir / "passphrase"
    if take:
        short = _short_disk(report)
        if short:
            raise BackupError(short)
        repo_text, passphrase = _ensure_default_repo(upgrade_dir)
        snapshot_id = _take(repo_text, passphrase, None, release, bifrost_data)
        logger.info(
            "pre-upgrade %s taken by %s snapshot %s in %s",
            release,
            __version__,
            snapshot_id,
            repo_text,
        )
    else:
        _log_skip(report)
    if passphrase.is_file() and (repo / "config").is_file():
        forget_pre_upgrade(str(repo), passphrase, None, upgrade_dir)
    return snapshot_id


def _snapshot_configured(
    dest: Destination,
    upgrade_dir: Path | None,
    report: dict,
    take: bool,
    release: str,
    bifrost_data: str | None,
) -> str | None:
    # A destination on its own filesystem is not judged by the upgrade dir.
    # Nothing to write and no local repo to prune: do not require its secrets.
    if not take and not _local_repo_ready(dest.repo):
        _log_skip(report)
        return None
    with prepared_destination(dest) as (passphrase, extra):
        snapshot_id = None
        if take:
            snapshot_id = _take(dest.repo, passphrase, extra, release, bifrost_data)
            logger.info(
                "pre-upgrade %s taken by %s snapshot %s in %s",
                release,
                __version__,
                snapshot_id,
                dest.name,
            )
        else:
            _log_skip(report)
        forget_pre_upgrade(dest.repo, passphrase, extra, upgrade_dir)
        return snapshot_id


def _local_repo_ready(repo: str) -> bool:
    local = _local_repo(repo)
    return local is not None and (local / "config").is_file()
