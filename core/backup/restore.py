"""Stage a snapshot, check it, and swap it in. ``--test`` discards the stage.

After a swap, rotate the JWT secret, re-encrypt MFA secrets, reject pending
approvals, and write one audit row. ``--test`` returns before any of that.

The dump is restored into a new database. ``agent_events_assign_hashes`` is a
BEFORE INSERT trigger and would re-hash rows if the dump landed in a schema
that already had it. Checks run before any rename. A failure before the swap
drops the stage and leaves the live database and files where they were.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import secrets
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import psycopg2
from cryptography.fernet import Fernet
from psycopg2 import sql

from core.backup.connection import backup_database_config
from core.backup.create import (
    PRE_RESTORE_PREFIX,
    STAGE_PREFIX,
    BackupError,
    _checked,
    _child_env,
    _conninfo_value,
    _output,
    _priority_prefix,
    _require_tool,
    _restic_env,
    _run,
)
from core.config import REPO_ROOT, get_settings, vigil_path
from core.intent import intent_file
from core.response.approval_service import ActionStatus
from core.secrets_manager import EncryptedFileBackend
from core.storage.config_service import get_config_service
from core.storage.connection import (
    DatabaseConfig,
    get_db_manager,
    init_database,
)
from core.storage.models import ApprovalAction, User
from core.time import utcnow
from core.version import __version__

# The FIRST_BREAK query from services/agent/ledger/verify.ts. The hash stays
# the database's agent_event_hash; $1 is the whole ledger (NULL run id).
_LEDGER_BREAK = """
SELECT run_id, seq,
       CASE
         WHEN prev_hash IS DISTINCT FROM expected_prev THEN 'prev_hash'
         ELSE 'payload'
       END AS reason
  FROM (
    SELECT run_id, seq, prev_hash, event_hash,
           coalesce(lag(event_hash) OVER (PARTITION BY run_id ORDER BY seq), '') AS expected_prev,
           agent_event_hash(prev_hash, payload) AS expected_hash
      FROM agent_events
     WHERE (%(run_id)s::uuid IS NULL OR run_id = %(run_id)s)
  ) c
 WHERE prev_hash IS DISTINCT FROM expected_prev
    OR event_hash IS DISTINCT FROM expected_hash
 ORDER BY run_id, seq
 LIMIT 1
"""

_APP_NAME = f"vigil-backup-{os.getpid()}"
_JWT_ENV_LINE = re.compile(r"^(\s*(?:export\s+)?JWT_SECRET_KEY\s*=\s*)(.*)$")


@dataclass
class _Placed:
    name: str
    staged: Path
    target: Path
    # A directory is swapped entry by entry and a file is rewritten in place.
    # Neither is ever renamed: under Compose they are mount points.
    is_dir: bool


@dataclass
class _Swapped:
    item: _Placed
    previous: Path | None = None
    aside: list[str] = field(default_factory=list)
    moved_in: list[str] = field(default_factory=list)
    created: bool = False


def restore_snapshot(
    *,
    repo: str,
    passphrase_file: str,
    snapshot: str,
    test: bool,
    actor: str | None = None,
    bifrost_data: str | None = None,
) -> str:
    passphrase = Path(passphrase_file)
    if not passphrase.is_file():
        raise BackupError(f"passphrase file not found: {passphrase_file}")
    _require_tool("restic")
    _require_tool("pg_restore")

    cfg = backup_database_config()
    priority = _priority_prefix()
    # Real restore only. --test never renames the live database, so other
    # sessions may stay connected.
    if not test:
        _assert_no_other_clients(cfg)

    restic = _restic(repo, test)
    manifest, dump_in_snapshot, snapshot_id = _load_manifest(
        restic, passphrase, snapshot, priority
    )
    _assert_compatible(manifest)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    staged_name = _suffixed(cfg.database, "_stage_", stamp)
    pre_name = _suffixed(cfg.database, "_pre_restore_", stamp)
    if staged_name == cfg.database or pre_name == cfg.database:
        raise BackupError(f"database name {cfg.database} is too long to restore")

    created = False
    swapped = False
    placed: list[_Placed] = []
    created_dirs: list[Path] = []
    error: BackupError | None = None
    try:
        _create_database(cfg, staged_name)
        created = True
        with tempfile.TemporaryDirectory(prefix="vigil-restore-") as raw:
            root = Path(raw)
            _checked(
                priority + [*restic, "restore", snapshot, "--target", str(root)],
                env=_restic_env(passphrase),
                what="restic restore",
            )
            dump_file = _under_target(root, dump_in_snapshot)
            if not dump_file.is_file():
                raise BackupError(f"snapshot has no dump at {dump_in_snapshot}")
            _pg_restore(cfg, staged_name, dump_file, priority)
            tables = manifest.get("tables")
            if not isinstance(tables, dict):
                raise BackupError("manifest has no table counts")
            _check_rows(cfg, staged_name, tables)
            _check_ledger(cfg, staged_name)
            _stage_locations(root, manifest, stamp, placed, created_dirs, bifrost_data)
            secrets = _check_secrets(placed)
            _check_schema(cfg, staged_name)
            lines = [
                f"rows: ok ({len(tables)} tables)",
                "ledger: ok",
                "schema: ok",
                f"secrets: {secrets}",
            ]
            if test:
                lines.append("discarded staged copies")
                return "\n".join(lines)
            lines.append(f"previous database: {pre_name}")
            lines.extend(_swap(cfg, staged_name, pre_name, placed, stamp))
            swapped = True
            created = False
            # The swap stays even when this fails. --test returned above. The
            # lines naming what it moved aside go out with the error.
            try:
                lines.extend(
                    _settle_restored(cfg, manifest, snapshot_id, _actor(actor))
                )
            except BackupError as exc:
                raise BackupError("\n".join([*lines, str(exc)])) from exc
            return "\n".join(lines)
    except BackupError as exc:
        error = exc
        raise
    except Exception as exc:
        error = BackupError(str(exc))
        raise error from exc
    finally:
        # A failed drop must not replace the check error that caused it.
        if not swapped:
            cleanup_error: Exception | None = None
            for item in placed:
                try:
                    _remove_path(item.staged)
                except Exception as exc:  # noqa: BLE001
                    cleanup_error = exc
            if created:
                try:
                    _drop_database(cfg, staged_name)
                except Exception as exc:  # noqa: BLE001
                    cleanup_error = exc
            for directory in sorted(
                created_dirs, key=lambda path: len(path.parts), reverse=True
            ):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            if cleanup_error is not None and error is None:
                raise BackupError(
                    f"could not remove the staged copies: {cleanup_error}"
                )


def _assert_no_other_clients(cfg: DatabaseConfig) -> None:
    conn = _connect_as(cfg, database=None, autocommit=True)
    try:
        rows = _other_clients(conn, None)
    finally:
        conn.close()
    if rows:
        raise BackupError(
            f"refusing restore: {len(rows)} other connection(s) on {cfg.database}"
        )


def _other_clients(
    conn: psycopg2.extensions.connection, datnames: list[str] | None
) -> list:
    # Client backends only. Autovacuum is not another session, and counting it
    # would refuse a quiet database whenever vacuum is running.
    cur = conn.cursor()
    if datnames is None:
        cur.execute(
            """
            SELECT pid
              FROM pg_stat_activity
             WHERE datname = current_database()
               AND backend_type = 'client backend'
               AND application_name IS DISTINCT FROM %s
            """,
            (_APP_NAME,),
        )
    else:
        cur.execute(
            """
            SELECT pid
              FROM pg_stat_activity
             WHERE datname = ANY(%s)
               AND backend_type = 'client backend'
               AND application_name IS DISTINCT FROM %s
            """,
            (datnames, _APP_NAME),
        )
    return cur.fetchall()


def _restic(repo: str, test: bool) -> list[str]:
    # --test only reads, so it takes no lock and a read-only repository works.
    return ["restic", "-r", repo, *(["--no-lock"] if test else [])]


def _load_manifest(
    restic: list[str], passphrase: Path, snapshot: str, priority: list[str]
) -> tuple[dict, str, str]:
    env = _restic_env(passphrase)
    listed = _checked(
        priority + [*restic, "ls", snapshot],
        env=env,
        what="restic ls",
    )
    paths = [
        line
        for line in listed.stdout.splitlines()
        if line and not line.startswith("snapshot ")
    ]
    present = set(paths)
    found: list[tuple[str, dict]] = []
    for dump in paths:
        if Path(dump).name != "db.dump":
            continue
        manifest_path = str(Path(dump).parent / "manifest.json")
        if manifest_path not in present:
            continue
        raw = _checked(
            priority + [*restic, "dump", snapshot, manifest_path],
            env=env,
            what="manifest",
        )
        try:
            payload = json.loads(raw.stdout)
        except json.JSONDecodeError:
            continue
        locations = payload.get("locations")
        if not isinstance(locations, list) or not isinstance(
            payload.get("tables"), dict
        ):
            continue
        database = next(
            (item for item in locations if item.get("name") == "database"), None
        )
        if isinstance(database, dict) and database.get("path") == dump:
            found.append((dump, payload))
    if len(found) != 1:
        raise BackupError("snapshot does not contain one Vigil manifest")
    return found[0][1], found[0][0], _snapshot_id(restic, env, snapshot, priority)


def _assert_compatible(manifest: dict) -> None:
    version = manifest.get("version")
    if not isinstance(version, str) or not version.strip():
        raise BackupError("manifest has no version")
    theirs = _release_pair(version)
    ours = _release_pair(__version__)
    if theirs != ours:
        raise BackupError(
            f"backup version {version} does not match this install "
            f"({__version__}); install {version}"
        )


def _release_pair(version: str) -> tuple[str, str]:
    core = version.strip().split("+", 1)[0].split("-", 1)[0]
    parts = core.split(".")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise BackupError(f"unreadable version {version}")
    return parts[0], parts[1]


def _suffixed(name: str, marker: str, stamp: str) -> str:
    suffix = f"{marker}{stamp}"
    if len(name) + len(suffix) <= 63:
        return name + suffix
    keep = 63 - len(suffix)
    if keep < 1:
        raise BackupError("restore names do not fit in a database identifier")
    return name[:keep] + suffix


def _create_database(cfg: DatabaseConfig, name: str) -> None:
    conn = _connect_as(cfg, database="postgres", autocommit=True)
    try:
        cur = conn.cursor()
        cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    except psycopg2.Error as exc:
        raise BackupError(f"could not create the staged database: {exc}") from exc
    finally:
        conn.close()


def _drop_database(cfg: DatabaseConfig, name: str) -> None:
    if name == cfg.database:
        raise BackupError("refusing to drop the live database")
    conn = _connect_as(cfg, database="postgres", autocommit=True)
    try:
        cur = conn.cursor()
        cur.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)
            )
        )
    except psycopg2.Error as exc:
        raise BackupError(f"could not drop the staged database: {exc}") from exc
    finally:
        conn.close()


def _pg_restore(
    cfg: DatabaseConfig, database: str, dump_file: Path, priority: list[str]
) -> None:
    env = _child_env()
    env["PGPASSWORD"] = cfg.password
    proc = _run(
        priority
        + [
            "pg_restore",
            "--single-transaction",
            "--exit-on-error",
            "--no-password",
            f"--dbname={_conninfo_for(cfg, database)}",
            str(dump_file),
        ],
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        raise BackupError(f"pg_restore failed: {_output(proc)}")


def _check_rows(cfg: DatabaseConfig, database: str, tables: dict) -> None:
    conn = _connect_as(cfg, database=database, autocommit=True)
    try:
        cur = conn.cursor()
        for key, expected in tables.items():
            if not isinstance(key, str) or "." not in key:
                raise BackupError(f"row count check failed: bad table name {key!r}")
            schema, table = key.split(".", 1)
            try:
                cur.execute(
                    sql.SQL("SELECT count(*) FROM {}.{}").format(
                        sql.Identifier(schema), sql.Identifier(table)
                    )
                )
                got = int(cur.fetchone()[0])
            except psycopg2.Error as exc:
                raise BackupError(f"row count check failed: {key}: {exc}") from exc
            if got != int(expected):
                raise BackupError(
                    f"row count mismatch for {key}: backup {expected}, staged {got}"
                )
    finally:
        conn.close()


def _check_ledger(cfg: DatabaseConfig, database: str) -> None:
    conn = _connect_as(cfg, database=database, autocommit=True)
    try:
        cur = conn.cursor()
        try:
            cur.execute(_LEDGER_BREAK, {"run_id": None})
            row = cur.fetchone()
        except psycopg2.Error as exc:
            raise BackupError(f"ledger check failed: {exc}") from exc
    finally:
        conn.close()
    if row:
        raise BackupError(
            f"ledger check failed: run={row[0]} seq={row[1]} reason={row[2]}"
        )


def _stage_locations(
    root: Path,
    manifest: dict,
    stamp: str,
    placed: list[_Placed],
    created_dirs: list[Path],
    bifrost_data: str | None,
) -> None:
    # Append as each location is moved. A later failure must still see the
    # siblings already staged, or the caller cannot delete them.
    settings = get_settings()
    for item in manifest.get("locations") or []:
        if not isinstance(item, dict) or item.get("status") != "included":
            continue
        name = item.get("name")
        if name == "database":
            continue
        if not isinstance(name, str):
            raise BackupError("manifest location has no name")
        original = item.get("path")
        if not isinstance(original, str) or not original:
            raise BackupError(f"{name} is included but has no path")
        src = _under_target(root, original)
        if not src.exists():
            raise BackupError(f"{name} is missing from the snapshot")
        target = _target_for(name, settings, bifrost_data)
        is_dir = src.is_dir()
        if target.exists() and target.is_dir() != is_dir:
            kind = "directory" if is_dir else "file"
            raise BackupError(f"{name}: {target} is not a {kind}")
        if not is_dir:
            # Copied from the restic target, which outlives the swap.
            _remember_created(target.parent, created_dirs)
            placed.append(_Placed(name, src, target, is_dir=False))
            continue
        # Staged inside the target so every later rename stays on its filesystem.
        _remember_created(target, created_dirs)
        dest = target / f"{STAGE_PREFIX}{stamp}"
        if dest.exists():
            raise BackupError(f"staging path already exists: {dest}")
        shutil.move(str(src), str(dest))
        placed.append(_Placed(name, dest, target, is_dir=True))


def _remember_created(parent: Path, created: list[Path]) -> None:
    missing: list[Path] = []
    cursor = parent
    while not cursor.exists() and cursor != cursor.parent:
        missing.append(cursor)
        cursor = cursor.parent
    parent.mkdir(parents=True, exist_ok=True)
    for path in missing:
        if path not in created:
            created.append(path)


def _target_for(name: str, settings, bifrost_data: str | None) -> Path:
    if name == "state_directory":
        return vigil_path()
    if name == "orchestrator_workdir":
        return Path(settings.orchestrator_workdir).resolve()
    if name == "skills":
        raw = settings.vigil_skills_path.strip()
        if not raw:
            raise BackupError("skills were backed up but VIGIL_SKILLS_PATH is unset")
        return Path(raw).resolve()
    if name == "intent":
        return intent_file().resolve()
    if name == "env":
        return (REPO_ROOT / ".env").resolve()
    if name == "bifrost":
        # The manifest path is a temporary staging copy, so the live directory
        # has to come from the caller.
        if not bifrost_data:
            raise BackupError(
                "the snapshot includes bifrost; pass --bifrost-data with its directory"
            )
        return Path(bifrost_data)
    raise BackupError(f"unknown location {name}")


def _under_target(root: Path, original: str) -> Path:
    path = Path(original)
    if path.is_absolute():
        path = Path(*path.parts[1:])
    return root / path


def _check_secrets(placed: list[_Placed]) -> str:
    state = next((item for item in placed if item.name == "state_directory"), None)
    if state is None:
        return "skipped"
    backend = EncryptedFileBackend(data_dir=state.staged)
    secrets_path = backend.secrets_path
    key_path = backend.master_key_path
    # _load_cache turns a bad blob into {} and _load_or_create_master_key
    # writes a key when master.key is absent. Neither is a successful check.
    if not secrets_path.is_file():
        return "ok"
    if not key_path.is_file():
        raise BackupError("secrets check failed: master.key is missing")
    key_bytes = key_path.read_bytes()
    try:
        plaintext = Fernet(key_bytes.strip()).decrypt(secrets_path.read_bytes())
        payload = json.loads(plaintext.decode("utf-8"))
    except Exception as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        raise BackupError(f"secrets check failed: {detail}") from exc
    if not isinstance(payload, dict):
        raise BackupError("secrets check failed: secrets.enc is not a JSON object")
    if key_path.read_bytes() != key_bytes:
        raise BackupError("secrets check failed: master.key was rewritten")
    return "ok"


def _check_schema(cfg: DatabaseConfig, database: str) -> None:
    manager = get_db_manager()
    if manager.engine is not None:
        manager.close()
    # init_database() always uses the process-wide manager. Point it at the
    # stage first: seeding the live database would change its row counts.
    manager.config = _config_for(cfg, database)
    try:
        init_database()
        report = get_db_manager().schema_report()
    except Exception as exc:
        raise BackupError(f"schema check failed: {exc}") from exc
    finally:
        get_db_manager().close()
    if report["state"] != "ok":
        raise BackupError(
            "schema check failed: "
            + json.dumps(
                {
                    "state": report["state"],
                    "missing_tables": report.get("missing_tables"),
                    "missing_columns": report.get("missing_columns"),
                    "not_null_columns": report.get("not_null_columns"),
                },
                sort_keys=True,
            )
        )


def _config_for(cfg: DatabaseConfig, database: str) -> DatabaseConfig:
    query = dict(cfg.extra_query)
    query["application_name"] = _APP_NAME
    if cfg.ssl_mode:
        query["sslmode"] = cfg.ssl_mode
    qs = "&".join(
        f"{key}={quote(str(value), safe='')}" for key, value in sorted(query.items())
    )
    user = quote(cfg.user, safe="")
    password = quote(cfg.password, safe="")
    host = quote(cfg.host, safe="")
    db = quote(database, safe="")
    dsn = f"postgresql://{user}:{password}@{host}:{cfg.port}/{db}?{qs}"
    return DatabaseConfig(connection_string=dsn)


def _swap(
    cfg: DatabaseConfig,
    staged_name: str,
    pre_name: str,
    placed: list[_Placed],
    stamp: str,
) -> list[str]:
    _close_our_backends(cfg, [cfg.database, staged_name])
    admin = _connect_as(cfg, database="postgres", autocommit=True)
    renamed = False
    try:
        if _other_clients(admin, [cfg.database, staged_name]):
            raise BackupError(
                f"refusing restore: other connection(s) on {cfg.database}"
            )
        _rename_database(admin, cfg.database, pre_name)
        renamed = True
        _rename_database(admin, staged_name, cfg.database)
    except BackupError:
        if renamed:
            _rename_database(admin, pre_name, cfg.database)
        raise
    except psycopg2.Error as exc:
        if renamed:
            _rename_database(admin, pre_name, cfg.database)
        raise BackupError(f"could not swap the database: {exc}") from exc
    finally:
        admin.close()
    try:
        return _swap_files(placed, stamp)
    except Exception:
        try:
            _rollback_database(cfg, staged_name, pre_name)
        except Exception as undo:
            raise BackupError(
                f"swap failed and the database rollback failed: {undo}"
            ) from undo
        raise


def _rollback_database(cfg: DatabaseConfig, staged_name: str, pre_name: str) -> None:
    admin = _connect_as(cfg, database="postgres", autocommit=True)
    try:
        _terminate_ours(admin, [cfg.database, staged_name, pre_name])
        _rename_database(admin, cfg.database, staged_name)
        _rename_database(admin, pre_name, cfg.database)
    finally:
        admin.close()


def _swap_files(placed: list[_Placed], stamp: str) -> list[str]:
    done: list[_Swapped] = []
    lines: list[str] = []
    pre_root = vigil_path() / f"{PRE_RESTORE_PREFIX}{stamp}"
    try:
        for item in placed:
            # Recorded before the first rename so a failure undoes what moved.
            swapped = _Swapped(item)
            done.append(swapped)
            if item.is_dir:
                _swap_dir(swapped, stamp)
            else:
                _swap_file(swapped, pre_root)
            if swapped.previous is not None:
                lines.append(f"previous {item.name}: {swapped.previous}")
            else:
                lines.append(f"restored {item.name}: {item.target}")
        # Kept until now so an undo has somewhere to put the staged entries.
        for swapped in done:
            if swapped.item.is_dir:
                swapped.item.staged.rmdir()
        return lines
    except Exception:
        _undo_files(done)
        raise


def _is_restore_artifact(name: str) -> bool:
    return name.startswith((STAGE_PREFIX, PRE_RESTORE_PREFIX))


def _swap_dir(swapped: _Swapped, stamp: str) -> None:
    item = swapped.item
    previous = item.target / f"{PRE_RESTORE_PREFIX}{stamp}"
    if previous.exists():
        raise BackupError(f"previous copy already exists: {previous}")
    previous.mkdir()
    swapped.previous = previous
    for entry in sorted(item.target.iterdir()):
        if _is_restore_artifact(entry.name):
            continue
        entry.rename(previous / entry.name)
        swapped.aside.append(entry.name)
    for entry in sorted(item.staged.iterdir()):
        entry.rename(item.target / entry.name)
        swapped.moved_in.append(entry.name)
    if not swapped.aside:
        previous.rmdir()
        swapped.previous = None


def _swap_file(swapped: _Swapped, pre_root: Path) -> None:
    item = swapped.item
    if item.target.is_file():
        previous = pre_root / item.name
        if previous.exists():
            raise BackupError(f"previous copy already exists: {previous}")
        pre_root.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item.target, previous)
        swapped.previous = previous
    else:
        swapped.created = True
    # In place, never a rename over the target: a bind-mounted file refuses it.
    shutil.copyfile(item.staged, item.target)
    if swapped.created:
        shutil.copymode(item.staged, item.target)


def _undo_files(done: list[_Swapped]) -> None:
    for swapped in reversed(done):
        item = swapped.item
        if not item.is_dir:
            if swapped.previous is not None:
                shutil.copyfile(swapped.previous, item.target)
                swapped.previous.unlink()
            elif swapped.created:
                item.target.unlink(missing_ok=True)
            if swapped.previous is not None:
                try:
                    swapped.previous.parent.rmdir()
                except OSError:
                    pass
            continue
        for name in reversed(swapped.moved_in):
            (item.target / name).rename(item.staged / name)
        if swapped.previous is None:
            continue
        for name in reversed(swapped.aside):
            (swapped.previous / name).rename(item.target / name)
        try:
            swapped.previous.rmdir()
        except OSError:
            pass


def _close_our_backends(cfg: DatabaseConfig, datnames: list[str]) -> None:
    get_db_manager().close()
    admin = _connect_as(cfg, database="postgres", autocommit=True)
    try:
        _terminate_ours(admin, datnames)
    finally:
        admin.close()


def _terminate_ours(conn: psycopg2.extensions.connection, datnames: list[str]) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT pg_terminate_backend(pid)
          FROM pg_stat_activity
         WHERE datname = ANY(%s)
           AND application_name = %s
           AND pid <> pg_backend_pid()
        """,
        (datnames, _APP_NAME),
    )


def _rename_database(conn: psycopg2.extensions.connection, old: str, new: str) -> None:
    cur = conn.cursor()
    cur.execute(
        sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
            sql.Identifier(old), sql.Identifier(new)
        )
    )


def _connect_as(
    cfg: DatabaseConfig, *, database: str | None, autocommit: bool
) -> psycopg2.extensions.connection:
    try:
        conn = psycopg2.connect(_conninfo_for(cfg, database), password=cfg.password)
    except psycopg2.Error as exc:
        raise BackupError(f"database connection failed: {exc}") from exc
    conn.autocommit = autocommit
    return conn


def _conninfo_for(cfg: DatabaseConfig, database: str | None) -> str:
    parts = {
        "host": cfg.host,
        "port": str(cfg.port),
        "user": cfg.user,
        "dbname": database or cfg.database,
        "sslmode": cfg.ssl_mode,
    }
    parts.update(dict(cfg.extra_query))
    parts["application_name"] = _APP_NAME
    parts.setdefault("connect_timeout", "10")
    return " ".join(
        f"{key}={_conninfo_value(str(value))}" for key, value in parts.items()
    )


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.is_symlink() or path.exists():
        path.unlink()


def _snapshot_id(
    restic: list[str], env: dict[str, str], snapshot: str, priority: list[str]
) -> str:
    # `restic ls` prints a short id. The audit and the rejection reason use
    # the full id `restic snapshots` returns, which is what `create` prints.
    raw = _checked(
        priority + [*restic, "snapshots", "--json", snapshot],
        env=env,
        what="restic snapshots",
    )
    try:
        rows = json.loads(raw.stdout)
    except json.JSONDecodeError as exc:
        raise BackupError(f"snapshot id could not be read: {exc}") from exc
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise BackupError("snapshot id could not be read")
    snapshot_id = rows[0].get("id")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise BackupError("snapshot id could not be read")
    return snapshot_id


def _actor(actor: str | None) -> str:
    if actor is not None and actor.strip():
        return actor.strip()
    try:
        return getpass.getuser()
    except Exception as exc:
        raise BackupError(f"could not identify the restoring user: {exc}") from exc


def _settle_restored(
    cfg: DatabaseConfig, manifest: dict, snapshot_id: str, actor: str
) -> list[str]:
    created_at = manifest.get("created_at")
    if not isinstance(created_at, str) or not created_at.strip():
        raise BackupError("manifest has no created_at")
    _point_at_live(cfg)
    old_key = _stored_jwt_secret()
    # Under Compose the backend takes the key from the environment, which the
    # operator also passes to this process. No store holds it.
    env_key = os.environ.get("JWT_SECRET_KEY")  # noqa: ENV001
    from_env = old_key is None and bool(env_key)
    if from_env:
        old_key = env_key
    new_key = secrets.token_urlsafe(48)
    expired = _reencrypt_and_expire(old_key, new_key, snapshot_id, created_at, actor)
    # With no key anywhere, a deployment outside this install sets it. A
    # jwt_secret written here would never be read.
    rotated = old_key is not None
    if rotated:
        _write_jwt_stores(new_key, secrets_only=from_env)
    get_config_service(user_id=actor).record_audit(
        config_type="backup",
        config_key="restore",
        action="restore",
        old_value=None,
        new_value={
            "snapshot_id": snapshot_id,
            "created_at": created_at,
            "expired_count": expired,
            "jwt_rotated": rotated,
        },
    )
    if not rotated:
        raise BackupError(
            f"approvals expired: {expired}\n"
            "JWT_SECRET_KEY is not in secrets.enc, either .env, or jwt_secret, so "
            "it is set outside this install; old sessions stay valid until it is "
            "rotated where it is defined"
        )
    lines = [
        f"backup date: {created_at}",
        f"approvals expired: {expired}",
        "integration credentials and user accounts date from the backup",
    ]
    if from_env:
        lines.append(
            "the new JWT_SECRET_KEY is in "
            f"{EncryptedFileBackend(data_dir=vigil_path()).secrets_path}; clear or "
            "replace JWT_SECRET_KEY in the environment Compose reads before `up`, "
            "because an environment value outranks secrets.enc"
        )
    return lines


def _point_at_live(cfg: DatabaseConfig) -> None:
    # _check_schema left the manager aimed at the staged name, which the swap
    # removed. close() does not clear that config.
    manager = get_db_manager()
    manager.close()
    # _config_for re-reads the platform proxy from secrets.enc. That file is
    # the restored copy now, and a proxy stored in the backup must not move
    # these writes off the server the swap just used.
    live = _config_for(cfg, cfg.database)
    live.proxy = cfg.proxy
    manager.config = live
    try:
        manager.initialize()
    except Exception as exc:
        raise BackupError(f"could not open the restored database: {exc}") from exc


def _stored_jwt_secret() -> str | None:
    # The next start.sh process, skipping this process's JWT_SECRET_KEY.
    # EnvironmentBackend outranks both .env files, so the env var must not win.
    state = vigil_path()
    encrypted = _encrypted_jwt(state)
    if encrypted:
        return encrypted
    for path in (REPO_ROOT / ".env", vigil_path(".env")):
        value = _env_jwt(path)
        if value:
            return value
    return _file_jwt(state / "jwt_secret")


def _encrypted_jwt(state: Path) -> str | None:
    backend = EncryptedFileBackend(data_dir=state)
    if not backend.secrets_path.is_file():
        return None
    value = backend.get("JWT_SECRET_KEY")
    return value or None


def _reencrypt_and_expire(
    old_key: str | None,
    new_key: str,
    snapshot_id: str,
    created_at: str,
    actor: str,
) -> int:
    reason = f"expired: restored from backup {snapshot_id} taken {created_at}"
    decided_at = utcnow()
    try:
        # auth_service reads JWT_SECRET_KEY at import and raises when it is
        # unset, which would stop `create` and `restore --test`.
        auth_service, auth_cls = _load_auth_service()
        with get_db_manager().session_scope() as session:
            users = session.query(User).filter(User.mfa_secret.isnot(None)).all()
            # Without the old key there is no rotation, so MFA stays as it is.
            if old_key:
                auth_service.JWT_SECRET_KEY = old_key
                plaintext = [
                    auth_cls._decrypt_mfa_secret(user.mfa_secret) for user in users
                ]
                auth_service.JWT_SECRET_KEY = new_key
                for user, secret in zip(users, plaintext):
                    user.mfa_secret = auth_cls._encrypt_mfa_secret(secret)
            pending = (
                session.query(ApprovalAction)
                .filter(ApprovalAction.status == ActionStatus.PENDING.value)
                .all()
            )
            for row in pending:
                row.status = ActionStatus.REJECTED.value
                row.rejection_reason = reason
                row.approved_by = actor
                row.approved_at = decided_at
            return len(pending)
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(f"could not settle the restored database: {exc}") from exc


def _load_auth_service():
    # A secret that lives only in jwt_secret is invisible to get_secret, and
    # this process's env is the wrong key to keep. The placeholder exists so
    # the import can finish; it is gone before the database work.
    placeholder_set = False
    if not os.environ.get("JWT_SECRET_KEY"):  # noqa: ENV001 - auth import boundary
        os.environ["JWT_SECRET_KEY"] = "restore-import-placeholder"  # noqa: ENV001
        placeholder_set = True
    try:
        import core.auth.auth_service as auth_service
        from core.auth.auth_service import AuthService
    finally:
        if placeholder_set:
            os.environ.pop("JWT_SECRET_KEY", None)  # noqa: ENV001
    return auth_service, AuthService


def _write_jwt_stores(new_key: str, *, secrets_only: bool = False) -> None:
    state = vigil_path()
    backend = EncryptedFileBackend(data_dir=state)
    # A key that came from the environment has no entry here yet, and no other
    # store held it, so the new one goes to secrets.enc alone.
    holds_key = backend.secrets_path.is_file() and backend.get("JWT_SECRET_KEY")
    if holds_key or secrets_only:
        if not backend.set("JWT_SECRET_KEY", new_key):
            raise BackupError("could not rotate JWT_SECRET_KEY in secrets.enc")
    if secrets_only:
        return
    seen: set[Path] = set()
    for path in (REPO_ROOT / ".env", vigil_path(".env")):
        key = path.resolve() if path.exists() else path
        if key in seen:
            continue
        seen.add(key)
        _edit_env_jwt(path, new_key)
    _write_jwt_file(state / "jwt_secret", new_key)


def _edit_env_jwt(path: Path, value: str) -> None:
    if not path.is_file():
        return
    original = path.read_bytes()
    text = original.decode("utf-8")
    newline = "\r\n" if b"\r\n" in original else "\n"
    lines = text.splitlines()
    changed = False
    for index, line in enumerate(lines):
        match = _JWT_ENV_LINE.match(line)
        if match is None:
            continue
        lines[index] = match.group(1) + _requote(match.group(2), value)
        changed = True
    if not changed:
        return
    body = newline.join(lines)
    if text.endswith(("\n", "\r\n")):
        body += newline
    path.write_bytes(body.encode("utf-8"))


def _requote(raw: str, value: str) -> str:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return f"{raw[0]}{value}{raw[0]}"
    return value


def _env_jwt(path: Path) -> str | None:
    # `source` keeps the last assignment. An empty one clears the variable.
    if not path.is_file():
        return None
    value: str | None = None
    seen = False
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _JWT_ENV_LINE.match(line)
        if match is None:
            continue
        seen = True
        raw = match.group(2).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        value = raw or None
    return value if seen else None


def _file_jwt(path: Path) -> str | None:
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def _write_jwt_file(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
