"""``python -m core.backup pre-upgrade``: snapshot before a new release touches the schema.

Run by Compose and ``start.sh`` ahead of everything that provisions the schema.
The snapshot goes to the default destination in ``backups.json`` and its
manifest carries the version the database is stamped at, so restoring it is
accepted by the release it was taken to protect.
"""

from __future__ import annotations

import logging
import os

from core.backup.connection import backup_database_config
from core.backup.create import BackupError, create_snapshot
from core.backup.restore import _release_pair
from core.backup.schedule import (
    DESTINATIONS_FILENAME,
    _load_destinations,
    prepared_destination,
)
from core.config import vigil_path
from core.storage.connection import read_schema_version
from core.version import __version__

logger = logging.getLogger(__name__)

SKIP_ENV = "VIGIL_SKIP_PREUPGRADE_BACKUP"
KIND = "pre-upgrade"


def pre_upgrade_snapshot(
    *, target_version: str | None = None, bifrost_data: str | None = None
) -> str | None:
    """The new snapshot id, or None when none is due. Raises BackupError on failure."""
    target = target_version or __version__
    if os.environ.get(SKIP_ENV, "").strip() == "1":  # noqa: ENV001
        logger.warning("%s=1: no pre-upgrade backup before %s", SKIP_ENV, target)
        return None
    try:
        return _snapshot(target, bifrost_data)
    except BackupError as exc:
        raise BackupError(f"{exc}; set {SKIP_ENV}=1 to start without one") from exc
    except Exception as exc:  # noqa: BLE001
        raise BackupError(
            f"pre-upgrade backup failed: {exc}; set {SKIP_ENV}=1 to start without one"
        ) from exc


def _snapshot(target: str, bifrost_data: str | None) -> str | None:
    stamp = read_schema_version(backup_database_config())
    if stamp is None:
        logger.info("database has no schema version; no pre-upgrade backup")
        return None
    try:
        same_release = _release_pair(stamp) == _release_pair(target)
    except BackupError as exc:
        # An image built without a release version ("dev") has nothing to compare.
        logger.warning("%s; no pre-upgrade backup for %s -> %s", exc, stamp, target)
        return None
    if same_release:
        logger.info(
            "database is at %s, target %s; no pre-upgrade backup", stamp, target
        )
        return None
    destinations = _load_destinations()
    if not destinations:
        # A file that holds entries but yields none is a broken config, not "none".
        path = vigil_path(DESTINATIONS_FILENAME)
        if path.is_file() and path.read_text(encoding="utf-8").strip() not in (
            "",
            "[]",
        ):
            raise BackupError(f"{DESTINATIONS_FILENAME} has no usable destination")
        logger.warning(
            "no backup destination in %s; upgrading %s to %s without a backup",
            DESTINATIONS_FILENAME,
            stamp,
            target,
        )
        return None
    dest = next((d for d in destinations if d.default), destinations[0])
    with prepared_destination(dest) as (passphrase, extra):
        snapshot_id = create_snapshot(
            repo=dest.repo,
            passphrase_file=str(passphrase),
            bifrost_data=bifrost_data,
            kind=KIND,
            tags=(KIND,),
            extra_env=extra,
            version=stamp,
        )
    logger.info(
        "pre-upgrade %s -> %s snapshot %s in %s", stamp, target, snapshot_id, dest.name
    )
    return snapshot_id
