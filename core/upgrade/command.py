"""``python -m core.upgrade``: the one schema writer, run as the database owner.

Holds a Postgres advisory lock for the whole run. ``--check`` takes the lock,
writes nothing and prints one JSON report. Apply goes ahead only when nothing
is pending and the version is unchanged (a restart), or with the restore-point
id just taken, or with ``VIGIL_UPGRADE_ACK_VERSION`` equal to this image's
version and a non-empty ``VIGIL_UPGRADE_ACK_REASON``. Each revision is its own
transaction. Apply outcomes other than a restart are appended to
``$VIGIL_UPGRADE_DIR/history.jsonl``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, TextIO

from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

from core.upgrade.schema import VERSION_TABLE, compare, schema_state
from core.version import __version__

PACKAGE_DIR = Path(__file__).resolve().parent
# The schema a database at head should have, as schema_state() reports it.
# tests/integration/test_upgrade_command.py checks it and says how to rewrite it.
HEAD_SCHEMA = PACKAGE_DIR / "head_schema.json"

LOCK_KEY = 0x7669_6769_6C55_5047  # "vigilUPG"
LOCK_WAIT_SECONDS = 15.0
LOCK_POLL_SECONDS = 0.5

EXIT_OK, EXIT_REFUSED, EXIT_BUSY, EXIT_FAILED = 0, 1, 2, 3

RESTART, APPLY, NEEDS_RESTORE_POINT, REFUSE = (
    "restart",
    "apply",
    "needs_restore_point",
    "refuse",
)
OVERRIDDEN = "overridden"


class UpgradeBusy(RuntimeError):
    """Another process holds the upgrade lock."""


@dataclass
class Plan:
    verdict: str
    reason: str
    revision: Optional[str] = None
    pending: list[str] = field(default_factory=list)
    previous_version: Optional[str] = None
    # The id passed in, or OVERRIDDEN for an acknowledgement.
    restore_point: Optional[str] = None


def alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(PACKAGE_DIR))
    return config


def script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(alembic_config())


@contextmanager
def upgrade_lock(engine: Engine) -> Iterator[None]:
    """Session advisory lock on its own connection, polled for LOCK_WAIT_SECONDS."""
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while not conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}
        ).scalar_one():
            if time.monotonic() >= deadline:
                raise UpgradeBusy("another upgrade is running")
            time.sleep(LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
    finally:
        conn.close()


def _previous_version(upgrade_dir: Optional[Path]) -> Optional[str]:
    """The ``to`` of the last ``applied`` history line; None when unknown."""
    if upgrade_dir is None:
        return None
    try:
        lines = (upgrade_dir / "history.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("outcome") == "applied":
            return entry.get("to")
    return None


def plan(
    conn,
    script: ScriptDirectory,
    *,
    restore_point: Optional[str],
    ack_version: str,
    ack_reason: str,
    upgrade_dir: Optional[Path],
) -> Plan:
    """What apply would do, from the database and the run history alone."""
    current = MigrationContext.configure(
        conn, opts={"version_table_schema": "public"}
    ).get_current_heads()
    if len(current) > 1:
        return Plan(REFUSE, f"database has more than one revision: {list(current)}")
    revision = current[0] if current else None
    if revision is not None:
        try:
            script.get_revision(revision)
        except CommandError:
            return Plan(
                REFUSE,
                f"database is at revision {revision}, which this image "
                f"({__version__}) does not know: the database is newer. Restore, "
                "or install a release at least as new as the database.",
                revision=revision,
            )
    else:
        tables = conn.execute(
            text(
                "SELECT count(*) FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename <> :t"
            ),
            {"t": VERSION_TABLE},
        ).scalar_one()
        if tables:
            return Plan(
                REFUSE,
                "the database has Vigil tables but no alembic_version; adopting "
                "an existing install is not supported by this build",
            )

    pending = [
        s.revision
        for s in reversed(list(script.iterate_revisions("heads", revision or "base")))
    ]
    previous = _previous_version(upgrade_dir)
    restart = Plan(
        RESTART,
        "nothing pending and the version is unchanged",
        revision=revision,
        pending=pending,
        previous_version=previous,
    )
    if not pending and previous in (None, __version__):
        return restart

    change = (
        f"{len(pending)} revision(s) pending"
        if pending
        else f"version changes from {previous} to {__version__}"
    )
    if restore_point:
        return replace(
            restart, verdict=APPLY, reason=change, restore_point=restore_point
        )
    if ack_version == __version__ and ack_reason.strip():
        return replace(restart, verdict=APPLY, reason=change, restore_point=OVERRIDDEN)
    stale = (
        f" (VIGIL_UPGRADE_ACK_VERSION={ack_version} is not {__version__})"
        if ack_version and ack_version != __version__
        else ""
    )
    return replace(
        restart,
        verdict=NEEDS_RESTORE_POINT,
        reason=f"{change}: pass --restore-point <id> of the restore point just "
        f"taken, or set VIGIL_UPGRADE_ACK_VERSION={__version__} and a non-empty "
        f"VIGIL_UPGRADE_ACK_REASON{stale}",
    )


def _tree_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).lstat().st_size
            except OSError:
                pass
    return total


def check_report(
    conn, current: Plan, state_dir: Optional[Path], upgrade_dir: Optional[Path]
) -> dict[str, Any]:
    """The --check JSON: verdict, reason, drift, pending, restore and free bytes."""
    drift: list[dict[str, Any]] = []
    # Only a database at a known head has an expected schema to compare with.
    if current.verdict != REFUSE and current.revision and not current.pending:
        expected = json.loads(HEAD_SCHEMA.read_text(encoding="utf-8"))
        drift = compare(expected, schema_state(conn))
    db_bytes = conn.execute(
        text("SELECT pg_database_size(current_database())")
    ).scalar_one()
    report: dict[str, Any] = {
        "verdict": current.verdict,
        "reason": current.reason,
        "drift": drift,
        "pending": current.pending,
        "restore_bytes": int(db_bytes) + (_tree_bytes(state_dir) if state_dir else 0),
    }
    if upgrade_dir is not None and upgrade_dir.is_dir():
        report["free_bytes"] = shutil.disk_usage(upgrade_dir).free
    return report


def _migrate(engine: Engine, script: ScriptDirectory) -> None:
    """Every pending revision, each in its own transaction."""

    def upgrade(rev, _context):
        return script._upgrade_revs("heads", rev)

    with engine.connect() as conn, EnvironmentContext(
        alembic_config(), script, fn=upgrade, destination_rev="heads"
    ) as env:
        env.configure(
            connection=conn,
            transaction_per_migration=True,
            version_table_schema="public",
        )
        with env.begin_transaction():
            env.run_migrations()


def _record(upgrade_dir: Optional[Path], current: Plan, outcome: str, reason: str):
    entry: dict[str, Any] = {
        "time": datetime.now(timezone.utc).isoformat(),
        "from": current.previous_version,
        "to": __version__,
        "outcome": outcome,
        "restore_point": current.restore_point,
        "reason": reason,
        "discarded": [],
    }
    if upgrade_dir is not None:
        upgrade_dir.mkdir(parents=True, exist_ok=True)
        with (upgrade_dir / "history.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    return entry


def run(
    url: str,
    *,
    check: bool = False,
    restore_point: Optional[str] = None,
    upgrade_dir: Optional[Path] = None,
    ack_version: str = "",
    ack_reason: str = "",
    state_dir: Optional[Path] = None,
    out: TextIO = sys.stdout,
) -> int:
    engine = create_engine(url, poolclass=NullPool)
    script = script_directory()

    def emit(payload: dict) -> None:
        print(json.dumps(payload), file=out)

    try:
        with upgrade_lock(engine):
            with engine.connect() as conn:
                current = plan(
                    conn,
                    script,
                    restore_point=restore_point,
                    ack_version=ack_version,
                    ack_reason=ack_reason,
                    upgrade_dir=upgrade_dir,
                )
                if check:
                    emit(check_report(conn, current, state_dir, upgrade_dir))
                    return (
                        EXIT_OK if current.verdict in (RESTART, APPLY) else EXIT_REFUSED
                    )

            if current.verdict == RESTART:
                emit({"outcome": RESTART, "reason": current.reason, "to": __version__})
                return EXIT_OK
            if current.verdict != APPLY:
                emit(_record(upgrade_dir, current, "refused", current.reason))
                return EXIT_REFUSED
            try:
                _migrate(engine, script)
            except Exception as e:  # noqa: BLE001 - recorded, then non-zero exit
                emit(
                    _record(upgrade_dir, current, "failed", f"{type(e).__name__}: {e}")
                )
                return EXIT_FAILED
            reason = ack_reason.strip() if current.restore_point == OVERRIDDEN else ""
            emit(_record(upgrade_dir, current, "applied", reason))
            return EXIT_OK
    except UpgradeBusy as e:
        emit({"outcome": "refused", "reason": str(e)})
        return EXIT_BUSY
    finally:
        engine.dispose()


def _default_url() -> str:
    from core.storage.connection import DatabaseConfig

    # POSTGRES_* only: the upgrade logs in as the owner, not the app's DSN.
    return DatabaseConfig(connection_string="").get_database_url()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m core.upgrade", description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="report only; write nothing"
    )
    parser.add_argument("--restore-point", help="id of the restore point just taken")
    parser.add_argument(
        "--database-url", help="owner connection URL (default: POSTGRES_* settings)"
    )
    args = parser.parse_args(argv)

    from core.config import get_settings, vigil_path

    settings = get_settings()
    return run(
        args.database_url or _default_url(),
        check=args.check,
        restore_point=args.restore_point,
        upgrade_dir=(
            Path(settings.vigil_upgrade_dir) if settings.vigil_upgrade_dir else None
        ),
        ack_version=settings.vigil_upgrade_ack_version,
        ack_reason=settings.vigil_upgrade_ack_reason,
        state_dir=vigil_path(),
    )
