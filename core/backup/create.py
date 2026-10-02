"""Create one encrypted restic snapshot and verify it before returning.

The database dump is ``pg_dump -Fc`` under a ``pg_export_snapshot()`` id, so
the manifest's row counts describe that dump. A second run exits without
queueing when the session advisory lock is already held.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from psycopg2 import sql

from core.backup.connection import backup_database_config
from core.config import REPO_ROOT, get_settings, vigil_path
from core.intent import intent_file
from core.storage.connection import DatabaseConfig
from core.version import __version__

# Session lock, distinct from services/api/routers/auth.py _BOOTSTRAP_LOCK.
LOCK_CLASSID = 1287
LOCK_OBJID = 1
SKIPPED_MESSAGE = "skipped: a backup is already running"

_ORDINARY_TABLES = """
SELECT n.nspname, c.relname
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r'
  AND n.nspname <> 'pg_catalog'
  AND n.nspname <> 'information_schema'
  AND n.nspname NOT LIKE 'pg_toast%%'
ORDER BY 1, 2
"""


class BackupError(Exception):
    """The run did not produce a verified snapshot."""


class BackupSkipped(BackupError):
    def __init__(self) -> None:
        super().__init__(SKIPPED_MESSAGE)


@dataclass(frozen=True)
class Location:
    name: str
    status: str
    reason: str | None = None
    path: str | None = None

    def as_dict(self) -> dict[str, str]:
        item = {"name": self.name, "status": self.status}
        if self.reason:
            item["reason"] = self.reason
        if self.path:
            item["path"] = self.path
        return item


def create_snapshot(
    *,
    repo: str,
    passphrase_file: str,
    bifrost_data: str | None,
    kind: str = "manual",
    tags: tuple[str, ...] = (),
) -> str:
    passphrase = Path(passphrase_file)
    if not passphrase.is_file():
        raise BackupError(f"passphrase file not found: {passphrase_file}")
    _require_tool("restic")
    _require_tool("pg_dump")
    _require_tool("pg_restore")

    cfg = backup_database_config()
    priority = _priority_prefix()
    lock = _connect(cfg, autocommit=True)
    try:
        if not _try_lock(lock):
            raise BackupSkipped()
        _require_pg_dump_version(lock, priority)
        _ensure_repo(repo, passphrase, priority)
        with tempfile.TemporaryDirectory(prefix="vigil-backup-") as raw:
            staging = Path(raw)
            dump_path = staging / "db.dump"
            snap_conn = _connect(cfg, autocommit=False)
            try:
                # The exported snapshot is valid only until this transaction ends.
                snapshot_id, counts = _export_and_count(snap_conn)
                _pg_dump(cfg, snapshot_id, dump_path, priority)
            finally:
                snap_conn.close()
            locations = _locations(staging, bifrost_data)
            manifest_path = staging / "manifest.json"
            _write_manifest(manifest_path, locations, counts, kind)
            paths = [str(manifest_path)]
            paths.extend(loc.path for loc in locations if loc.path)
            snap = _restic_backup(repo, passphrase, paths, priority, tags)
            try:
                _verify(
                    repo,
                    passphrase,
                    snap,
                    dump_path,
                    manifest_path,
                    locations,
                    priority,
                )
            except BackupError as exc:
                # restic tag rewrites the snapshot id, so the tag is set on
                # backup and a failed verify removes that snapshot instead.
                detail = _forget_snapshot(repo, passphrase, snap, priority)
                if detail:
                    raise BackupError(
                        f"{exc}; failed to remove snapshot {snap}: {detail}"
                    ) from exc
                raise
            return snap
    finally:
        lock.close()


_PG_MIN_MAJOR = 16
# Set when a client >= 16 is found off PATH. Child processes see it via
# _child_env; resolving it must not execute the binary.
_pg_bindir: str | None = None


def _require_tool(name: str) -> None:
    if name in ("pg_dump", "pg_restore"):
        resolved = _pg_client(name)
    else:
        resolved = shutil.which(name)
    if resolved is None:
        raise BackupError(f"{name} is not installed")


def _pg_client(name: str) -> str | None:
    """A pg_dump/pg_restore whose major is at least 16, without running it.

    postgresql-client-16 installs under /usr/lib/postgresql/16/bin, which is
    not on PATH. The directory name is the major, so an older client already
    on PATH does not win. When that directory is absent, whatever ``which``
    finds is used and ``_require_pg_dump_version`` still rejects one older
    than the server.
    """
    global _pg_bindir
    root = Path("/usr/lib/postgresql")
    best: tuple[int, Path] | None = None
    if root.is_dir():
        for path in root.glob(f"*/bin/{name}"):
            if not os.access(path, os.X_OK):
                continue
            major_text = path.relative_to(root).parts[0]
            if not major_text.isdigit():
                continue
            major = int(major_text)
            if major < _PG_MIN_MAJOR:
                continue
            if best is None or major > best[0]:
                best = (major, path)
    if best is not None:
        _pg_bindir = str(best[1].parent)
        return str(best[1])
    return shutil.which(name)


def _pg_major(text: str) -> int | None:
    marker = "(PostgreSQL)"
    if marker not in text:
        return None
    head = text.split(marker, 1)[1].strip().split(".", 1)[0]
    if not head.isdigit():
        return None
    return int(head)


def _priority_prefix() -> list[str]:
    prefix: list[str] = []
    if shutil.which("nice"):
        prefix.extend(["nice", "-n", "19"])
    ionice = shutil.which("ionice")
    if ionice is None:
        return prefix
    probe = subprocess.run([ionice, "-c", "3", "true"], capture_output=True)
    if probe.returncode == 0:
        prefix.extend([ionice, "-c", "3"])
    return prefix


def _connect(
    cfg: DatabaseConfig, *, autocommit: bool
) -> psycopg2.extensions.connection:
    try:
        conn = psycopg2.connect(_conninfo(cfg), password=cfg.password)
    except psycopg2.Error as exc:
        raise BackupError(f"database connection failed: {exc}") from exc
    conn.autocommit = autocommit
    return conn


def _conninfo(cfg: DatabaseConfig) -> str:
    parts = {
        "host": cfg.host,
        "port": str(cfg.port),
        "user": cfg.user,
        "dbname": cfg.database,
        "sslmode": cfg.ssl_mode,
    }
    parts.update(dict(cfg.extra_query))
    parts.setdefault("connect_timeout", "10")
    return " ".join(f"{key}={_conninfo_value(str(val))}" for key, val in parts.items())


def _conninfo_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _try_lock(conn: psycopg2.extensions.connection) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT pg_try_advisory_lock(%s, %s)", (LOCK_CLASSID, LOCK_OBJID))
    row = cur.fetchone()
    return bool(row and row[0])


def _require_pg_dump_version(
    conn: psycopg2.extensions.connection, priority: list[str]
) -> None:
    cur = conn.cursor()
    cur.execute("SELECT current_setting('server_version_num')")
    server_major = int(cur.fetchone()[0]) // 10000
    proc = _run(priority + ["pg_dump", "--version"], env=_child_env())
    text = proc.stdout
    major = _pg_major(text)
    if major is None:
        raise BackupError(f"could not read pg_dump version: {text.strip()}")
    if major < server_major:
        raise BackupError(f"pg_dump {major} is older than the server ({server_major})")


def _ensure_repo(repo: str, passphrase: Path, priority: list[str]) -> None:
    """Open the repository, or init it, before any dump is taken."""
    env = _restic_env(passphrase)
    local = _local_repo(repo)
    if local is not None and (local / "config").is_file():
        _run(priority + ["restic", "-r", repo, "cat", "config"], env=env)
        return
    probe = _run(
        priority + ["restic", "-r", repo, "cat", "config"],
        env=env,
        check=False,
    )
    if probe.returncode == 0:
        return
    detail = _output(probe)
    if "wrong password" in detail.lower() or "no key found" in detail.lower():
        raise BackupError(detail)
    init = _run(priority + ["restic", "-r", repo, "init"], env=env, check=False)
    if init.returncode != 0:
        raise BackupError(_output(init) or detail)


def _local_repo(repo: str) -> Path | None:
    # Scheme-qualified targets (s3:, rest:) are passed through unchanged.
    if "://" in repo or (":" in repo and not repo.startswith("/")):
        return None
    return Path(repo)


def _export_and_count(
    conn: psycopg2.extensions.connection,
) -> tuple[str, dict[str, int]]:
    cur = conn.cursor()
    cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    cur.execute("SHOW transaction_isolation")
    isolation = cur.fetchone()[0]
    if isolation != "repeatable read":
        raise BackupError(f"database snapshot isolation is {isolation}")
    cur.execute("SELECT pg_export_snapshot()")
    snapshot_id = cur.fetchone()[0]
    cur.execute(_ORDINARY_TABLES)
    tables = cur.fetchall()
    counts: dict[str, int] = {}
    for schema, table in tables:
        cur.execute(
            sql.SQL("SELECT count(*) FROM {}.{}").format(
                sql.Identifier(schema), sql.Identifier(table)
            )
        )
        counts[f"{schema}.{table}"] = int(cur.fetchone()[0])
    return snapshot_id, counts


def _pg_dump(
    cfg: DatabaseConfig, snapshot_id: str, dump_path: Path, priority: list[str]
) -> None:
    env = _child_env()
    env["PGPASSWORD"] = cfg.password
    proc = _run(
        priority
        + [
            "pg_dump",
            "--format=custom",
            f"--snapshot={snapshot_id}",
            "--no-password",
            "--file",
            str(dump_path),
            _conninfo(cfg),
        ],
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise BackupError(f"pg_dump failed: {_output(proc)}")
    if not dump_path.is_file():
        raise BackupError("pg_dump wrote no dump file")


def _locations(staging: Path, bifrost_data: str | None) -> list[Location]:
    settings = get_settings()
    locations = [
        Location("database", "included", path=str(staging / "db.dump")),
        _existing("state_directory", vigil_path(), directory=True),
        _existing(
            "orchestrator_workdir",
            Path(settings.orchestrator_workdir),
            directory=True,
        ),
        _skills(settings.vigil_skills_path),
        _file_or_absent("intent", intent_file()),
        _file_or_absent("env", REPO_ROOT / ".env"),
        _bifrost(staging, bifrost_data),
    ]
    return locations


def _existing(name: str, path: Path, *, directory: bool) -> Location:
    present = path.is_dir() if directory else path.exists()
    if present:
        return Location(name, "included", path=str(path))
    return Location(name, "skipped", reason="absent")


def _skills(raw: str) -> Location:
    if not raw.strip():
        return Location("skills", "skipped", reason="unset")
    return _existing("skills", Path(raw), directory=False)


def _file_or_absent(name: str, path: Path) -> Location:
    if path.is_file():
        return Location(name, "included", path=str(path))
    return Location(name, "skipped", reason="absent")


def _bifrost(staging: Path, bifrost_data: str | None) -> Location:
    if bifrost_data is None:
        return Location("bifrost", "skipped", reason="unset")
    src = Path(bifrost_data)
    if not src.is_dir():
        return Location("bifrost", "skipped", reason="absent")
    dest = staging / "bifrost"
    _stage_bifrost(src, dest)
    return Location("bifrost", "included", path=str(dest))


def _stage_bifrost(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if _skip_bifrost(rel.name):
            continue
        target = dest / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.name.endswith(".db"):
            _sqlite_backup(path, target)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def _skip_bifrost(name: str) -> bool:
    return (
        name == "logs.db"
        or name.startswith("logs.db-")
        or name.endswith(".db-wal")
        or name.endswith(".db-shm")
    )


def _sqlite_backup(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(src)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
    except sqlite3.Error as exc:
        raise BackupError(f"sqlite backup of {src} failed: {exc}") from exc
    finally:
        source.close()


def _write_manifest(
    path: Path, locations: list[Location], counts: dict[str, int], kind: str
) -> None:
    payload = {
        "version": __version__,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "locations": [loc.as_dict() for loc in locations],
        "tables": counts,
    }
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _verify(
    repo: str,
    passphrase: Path,
    snapshot_id: str,
    dump_path: Path,
    manifest_path: Path,
    locations: list[Location],
    priority: list[str],
) -> None:
    env = _restic_env(passphrase)
    _checked(priority + ["restic", "-r", repo, "check"], env=env, what="restic check")
    _checked(
        priority + ["restic", "-r", repo, "dump", "--archive", "tar", snapshot_id, "/"],
        env=env,
        what="restic dump",
        stdout=subprocess.DEVNULL,
    )
    _restore_list(repo, env, snapshot_id, dump_path, priority)
    raw = _checked(
        priority + ["restic", "-r", repo, "dump", snapshot_id, str(manifest_path)],
        env=env,
        what="manifest",
    )
    try:
        manifest = json.loads(raw.stdout)
    except json.JSONDecodeError as exc:
        raise BackupError(
            f"verification failed: manifest does not parse: {exc}"
        ) from exc
    named = {
        item["name"]
        for item in manifest.get("locations", [])
        if item.get("status") == "included"
    }
    expected = {loc.name for loc in locations if loc.status == "included"}
    if named != expected:
        raise BackupError(
            "verification failed: manifest locations "
            f"{sorted(named)} != {sorted(expected)}"
        )


def _restore_list(
    repo: str,
    env: dict[str, str],
    snapshot_id: str,
    dump_path: Path,
    priority: list[str],
) -> None:
    # Custom-format TOC sits at the end of the archive. A pipe makes pg_restore
    # stop early and SIGPIPE restic, which leaves the repository lock behind.
    with tempfile.TemporaryDirectory(prefix="vigil-backup-dump-") as raw:
        copy = Path(raw) / "db.dump"
        with copy.open("wb") as fh:
            dumped = subprocess.run(
                priority + ["restic", "-r", repo, "dump", snapshot_id, str(dump_path)],
                env=env,
                stdout=fh,
                stderr=subprocess.PIPE,
            )
        if dumped.returncode != 0:
            detail = dumped.stderr.decode(errors="replace").strip()
            raise BackupError(f"verification failed: restic dump: {detail}")
        restore = subprocess.run(
            priority + ["pg_restore", "--list", str(copy)],
            env=_child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    if restore.returncode != 0 or not restore.stdout.strip():
        raise BackupError(
            f"verification failed: pg_restore --list: {restore.stderr.strip()}"
        )


def _restic_backup(
    repo: str,
    passphrase: Path,
    paths: list[str],
    priority: list[str],
    tags: tuple[str, ...],
) -> str:
    tagged: list[str] = []
    for tag in tags:
        tagged.extend(["--tag", tag])
    proc = _run(
        priority + ["restic", "-r", repo, "backup", "--json", *tagged, *paths],
        env=_restic_env(passphrase),
        check=False,
    )
    if proc.returncode != 0:
        raise BackupError(f"restic backup failed: {_output(proc)}")
    snapshot_id = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("message_type") == "summary" and payload.get("snapshot_id"):
            snapshot_id = payload["snapshot_id"]
    if not snapshot_id:
        raise BackupError("restic backup did not report a snapshot id")
    return snapshot_id


def _forget_snapshot(
    repo: str, passphrase: Path, snapshot_id: str, priority: list[str]
) -> str | None:
    """Remove one snapshot. The detail is None when restic succeeded."""
    proc = _run(
        priority + ["restic", "-r", repo, "forget", snapshot_id],
        env=_restic_env(passphrase),
        check=False,
    )
    if proc.returncode != 0:
        return _output(proc) or "restic forget failed"
    return None


def _child_env() -> dict[str, str]:
    # pg_dump and restic need PATH and locale. Config is not read from here.
    env = os.environ.copy()  # noqa: ENV001 - child process env
    if _pg_bindir:
        env["PATH"] = _pg_bindir + os.pathsep + env.get("PATH", "")
    return env


def _restic_env(passphrase: Path) -> dict[str, str]:
    env = _child_env()
    env["RESTIC_PASSWORD_FILE"] = str(passphrase)
    return env


def _checked(
    args: list[str],
    *,
    env: dict[str, str],
    what: str,
    stdout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = _run(args, env=env, check=False, stdout=stdout)
    if proc.returncode != 0:
        raise BackupError(f"verification failed: {what}: {_output(proc)}")
    return proc


def _run(
    args: list[str],
    *,
    env: dict[str, str],
    check: bool = True,
    stdout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        args,
        env=env,
        stdout=subprocess.PIPE if stdout is None else stdout,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and proc.returncode != 0:
        raise BackupError(_output(proc) or f"{args[0]} failed")
    return proc


def _output(proc: subprocess.CompletedProcess[str]) -> str:
    return (proc.stderr or proc.stdout or "").strip()
