"""Stage a snapshot, check it, and swap it in. ``--test`` discards the stage.

After a swap, rotate the JWT secret, re-encrypt MFA secrets, reject pending
approvals, and write one trail entry. ``--test`` returns before any of that.
A snapshot from before the trail still gets the config audit row.

The dump is restored into a new database. ``agent_events_assign_hashes`` and
``audit_trail_assign`` are BEFORE INSERT triggers and would re-hash rows if
the dump landed in a schema that already had them. Checks run before any
rename. A failure before the swap drops the stage and leaves the live
database and files where they were.

Bifrost's request log (``logs.db`` and its ``-wal``/``-shm`` files) lives in the
Bifrost data directory but is never in a snapshot. The swap moves the live one
aside with the rest of that directory, into ``.pre-restore-<stamp>``, and
Bifrost starts a fresh, empty log. Copy it back from there to keep the history.
"""

from __future__ import annotations

import base64
import getpass
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import psycopg2
from cryptography.fernet import Fernet, InvalidToken
from psycopg2 import sql
from sqlalchemy import create_engine
from sqlalchemy import text as sa_text
from sqlalchemy.pool import NullPool

from core.audit import feed, retention, trail
from core.auth import dormancy, key_ring, sign_in_runs, sign_ins
from core.auth.mfa_coverage import SETTINGS_LINK
from core.backup.connection import backup_database_config
from core.backup.create import (
    PAIR_PREFIX,
    PRE_RESTORE_PREFIX,
    STAGE_PREFIX,
    WRAPPING_FILE,
    WRAPPING_TAG,
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
from core.skills.skill_library import operator_skills_root
from core.storage.config_service import get_config_service
from core.storage.connection import DatabaseConfig, get_db_manager
from core.storage.models import ApprovalAction, SignIn, User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow
from core.version import __version__

logger = logging.getLogger(__name__)

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
    discarded: list[str] | None = None,
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
    snapshot = _resolve_snapshot(restic, passphrase, snapshot, priority)
    manifest, dump_in_snapshot, snapshot_id = _load_manifest(
        restic, passphrase, snapshot, priority
    )
    _assert_compatible(manifest)
    # Before anything is staged. A missing date is not a version mismatch.
    created_at = manifest.get("created_at")
    if not isinstance(created_at, str) or not created_at.strip():
        raise BackupError("manifest has no created_at")
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
            lines = [
                f"rows: ok ({len(tables)} tables)",
                "ledger: ok",
                f"secrets: {secrets}",
            ]
            if test:
                lines.append("discarded staged copies")
                return "\n".join(lines)
            lines.append(f"previous database: {pre_name}")
            # Resolved before the swap: an unknown operator refuses the restore
            # with nothing moved, rather than failing after the swap.
            restoring_user = _actor(actor)
            _check_erasure_list(root, dump_in_snapshot)
            paired_key = _paired_wrapping_key(
                restic, passphrase, snapshot_id, priority
            )
            swap_lines, abandoned, live_ring = _swap(
                cfg, staged_name, pre_name, placed, stamp
            )
            lines.extend(swap_lines)
            swapped = True
            created = False
            # The swap stays even when this fails. --test returned above. The
            # lines naming what it moved aside go out with the error. History
            # is recorded after settle so a missing history file does not skip
            # JWT rotation, and a write error still carries those lines.
            try:
                # The restored database is the live name. Point this process at
                # it before blanking, or the erasure list is applied nowhere.
                _point_at_live(cfg)
                reapplied_at = utcnow()
                _reapply_erased(root, dump_in_snapshot)
                lines.extend(
                    _settle_restored(
                        cfg,
                        manifest,
                        snapshot_id,
                        restoring_user,
                        abandoned,
                        reapplied_at,
                        paired_key,
                        live_ring,
                    )
                )
                _record_restored(manifest, snapshot_id, discarded)
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


def _snapshots(restic: list[str], passphrase: Path, priority: list[str]) -> list[dict]:
    raw = _checked(
        priority + [*restic, "snapshots", "--json"],
        env=_restic_env(passphrase),
        what="restic snapshots",
    )
    try:
        listed = json.loads(raw.stdout)
    except json.JSONDecodeError as exc:
        raise BackupError(f"snapshots could not be read: {exc}") from exc
    if not isinstance(listed, list):
        return []
    return [s for s in listed if isinstance(s, dict)]


def _resolve_snapshot(
    restic: list[str], passphrase: Path, snapshot: str, priority: list[str]
) -> str:
    """``latest`` is the newest backup, not the newest snapshot: the wrapping-key
    snapshot written after each backup is never what a restore wants."""
    if snapshot != "latest":
        return snapshot
    mains = [
        s
        for s in _snapshots(restic, passphrase, priority)
        if WRAPPING_TAG not in (s.get("tags") or [])
    ]
    if not mains:
        raise BackupError("the repository has no backup to restore")
    return max(mains, key=lambda s: str(s.get("time")))["id"]


def _paired_wrapping_key(
    restic: list[str], passphrase: Path, snapshot_id: str, priority: list[str]
) -> str | None:
    """The JWT_SECRET_KEY backed up with ``snapshot_id``, or None for a backup
    taken before they were paired (or whose pairing could not be read)."""
    try:
        for snap in _snapshots(restic, passphrase, priority):
            tags = snap.get("tags") or []
            if WRAPPING_TAG not in tags:
                continue
            paired = [t[len(PAIR_PREFIX) :] for t in tags if t.startswith(PAIR_PREFIX)]
            # A short id and a full one name one snapshot when one starts the other.
            if not any(
                snapshot_id.startswith(p) or p.startswith(snapshot_id) for p in paired
            ):
                continue
            raw = _checked(
                priority + [*restic, "dump", snap["id"], f"/{WRAPPING_FILE}"],
                env=_restic_env(passphrase),
                what="wrapping-key snapshot",
            )
            return raw.stdout.strip() or None
    except BackupError as exc:
        logger.warning("could not read the paired wrapping key: %s", exc)
    return None


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
    if manifest.get("kind") == "pre-upgrade":
        taken_by = manifest.get("taken_by")
        floor = taken_by if isinstance(taken_by, str) and taken_by.strip() else version
        if not _version_at_least(__version__, floor):
            raise BackupError(
                f"backup taken by {floor} is newer than this install "
                f"({__version__}); install {floor}"
            )
        return
    theirs = _release_pair(version)
    ours = _release_pair(__version__)
    if theirs != ours:
        raise BackupError(
            f"backup version {version} does not match this install "
            f"({__version__}); install {version}"
        )


def _version_at_least(ours: str, floor: str) -> bool:
    """Full version compare. Identical strings match, including an unparsed dev."""
    if ours.strip() == floor.strip():
        return True
    left, right = _version_key(ours), _version_key(floor)
    width = max(len(left), len(right))
    left = left + (0,) * (width - len(left))
    right = right + (0,) * (width - len(right))
    return left >= right


def _version_key(version: str) -> tuple[int, ...]:
    core = version.strip().split("+", 1)[0].split("-", 1)[0]
    parts = [part for part in core.split(".") if part != ""]
    if not parts or any(not part.isdigit() for part in parts):
        raise BackupError(f"unreadable version {version}")
    return tuple(int(part) for part in parts)


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
        outer = _enclosing(placed, target)
        if outer is not None:
            # The default skills folder sits in the State Directory, whose swap
            # moves every entry aside, a staged skills copy with them. So it
            # rides in that directory's staged copy instead, and wins over a
            # skills/ folder the snapshot holds there: it was the live root.
            dest = outer.staged / target.relative_to(outer.target.resolve())
            if dest.exists():
                shutil.rmtree(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            continue
        if name == "env":
            _keep_live_jwt(src, target)
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


def _keep_live_jwt(staged: Path, live: Path) -> None:
    """A snapshot's .env from #4181 on has no JWT_SECRET_KEY line, and must not
    take the install's secret away: carry the live file's line over. An older
    snapshot's .env still has its own, as before."""
    if not live.is_file() or _env_jwt(staged) is not None:
        return
    mine = [
        line
        for line in live.read_text(encoding="utf-8").splitlines(keepends=True)
        if _JWT_ENV_LINE.match(line.rstrip("\r\n"))
    ]
    if not mine:
        return
    body = staged.read_text(encoding="utf-8")
    if body and not body.endswith("\n"):
        body += "\n"
    staged.write_text(body + "".join(mine), encoding="utf-8")


def _enclosing(placed: list[_Placed], target: Path) -> _Placed | None:
    """A directory already staged whose target strictly holds ``target``, if any."""
    inner = target.resolve()
    for item in placed:
        outer = item.target.resolve()
        if item.is_dir and inner != outer and inner.is_relative_to(outer):
            return item
    return None


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
        root = operator_skills_root(settings)
        if root is None:
            raise BackupError(
                "skills were backed up but operator skills are off "
                "(VIGIL_SKILLS_PATH is empty)"
            )
        return root.resolve()
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


def _check_erasure_list(root: Path, dump_in_snapshot: str) -> None:
    """Refuse before the swap when the snapshot's erasures could not be re-applied."""
    path = _under_target(root, str(Path(dump_in_snapshot).parent / retention.LIST_NAME))
    if path.is_file() and retention.read_ids(path) and retention.outside_path() is None:
        raise BackupError(
            "the snapshot erases principals and VIGIL_UPGRADE_DIR is unset; "
            "set it, then restore again"
        )


def _reapply_erased(root: Path, dump_in_snapshot: str) -> None:
    """Union the snapshot's erasure list with the outside file and blank each id."""
    path = _under_target(root, str(Path(dump_in_snapshot).parent / retention.LIST_NAME))
    try:
        with unit_of_work() as session:
            retention.reapply(
                session, retention.read_ids(path) if path.is_file() else []
            )
    except Exception as exc:
        raise BackupError(f"could not reapply erased principals: {exc}") from exc


def _record_restored(
    manifest: dict, snapshot_id: str, discarded: list[str] | None
) -> None:
    """One history line, same keys as ``core.upgrade.command._record``.

    Written only when ``VIGIL_UPGRADE_DIR`` is set. ``--test`` never reaches
    here: it returns before the swap.
    """
    raw = (get_settings().vigil_upgrade_dir or "").strip()
    if not raw:
        return
    version = manifest.get("version")
    entry = {
        "time": datetime.now(timezone.utc).isoformat(),
        "from": __version__,
        "to": version if isinstance(version, str) else None,
        "outcome": "restored",
        "restore_point": snapshot_id,
        "reason": "",
        "discarded": list(discarded or []),
    }
    path = Path(raw)
    try:
        path.mkdir(parents=True, exist_ok=True)
        with (path / "history.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError as exc:
        raise BackupError(f"could not record the restore: {exc}") from exc


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
) -> tuple[list[str], dict | None, dict]:
    _close_our_backends(cfg, [cfg.database, staged_name])
    # Read and release before the rename. A connection left on the live
    # database makes _other_clients refuse the swap.
    abandoned = _abandoned_tip(cfg)
    live_ring = _live_ring(cfg)
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
        return _swap_files(placed, stamp), abandoned, live_ring
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


# The trail table, not only its revision: ``stamp_schema_head`` stamps a
# ``create_all`` database that has no ``audit_trail``.
_trail_ready = trail.in_use


def _abandoned_tip(cfg: DatabaseConfig) -> dict | None:
    """The live trail's tip, or None when the database has no trail.

    An empty ``audit_trail`` falls back to the anchor, as ``trail.current_tip``
    does, so a prefix delete that removed every row still names the tip it
    abandoned. The engine is disposed before return so the rename is not
    refused for this connection.
    """
    engine = create_engine(
        _config_for(cfg, cfg.database).get_database_url(), poolclass=NullPool
    )
    try:
        with engine.connect() as conn:
            if not _trail_ready(conn):
                return None
            tip = trail.current_tip(conn)
    finally:
        engine.dispose()
    return {"seq": int(tip.seq), "hash": str(tip.entry_hash)}


def _live_ring(cfg: DatabaseConfig) -> dict:
    """What the live key ring knows, read before the swap: the retired (purpose,
    version) pairs, which a restore must keep retired, and the highest version of
    each purpose, which it must not reuse. Disposed before return, as above."""
    engine = create_engine(
        _config_for(cfg, cfg.database).get_database_url(), poolclass=NullPool
    )
    try:
        with engine.connect() as conn:
            if not conn.execute(
                sa_text("SELECT to_regclass('public.auth_keys') IS NOT NULL")
            ).scalar_one():
                return {"retired": [], "highest": {}}
            retired = conn.execute(
                sa_text(
                    "SELECT purpose, version FROM public.auth_keys "
                    "WHERE state = 'retired'"
                )
            ).all()
            highest = conn.execute(
                sa_text(
                    "SELECT purpose, max(version) FROM public.auth_keys "
                    "GROUP BY purpose"
                )
            ).all()
    finally:
        engine.dispose()
    return {
        "retired": [(p, v) for p, v in retired],
        "highest": {p: v for p, v in highest},
    }


def _restore_values(
    snapshot_id: str,
    created_at: str,
    expired: int,
    rotated: bool,
    ended: int,
    stopped: int,
    mfa_reset: int,
) -> dict:
    return {
        "snapshot_id": snapshot_id,
        "created_at": created_at,
        "expired_count": expired,
        "jwt_rotated": rotated,
        "sign_ins_ended": ended,
        "runs_stopped": stopped,
        "mfa_reset": mfa_reset,
    }


def _record_restore(
    snapshot_id: str,
    created_at: str,
    expired: int,
    rotated: bool,
    ended: int,
    stopped: int,
    mfa_reset: int,
    actor: str,
    abandoned: dict | None,
    failure: BackupError | None,
    legacy: bool,
) -> None:
    """One trail entry, or today's config audit row when the trail is absent.

    The writer does not set ``prev_hash`` or ``seq``: the trigger chains onto
    the restored tip. The OS user sits in ``operator_text``, not the actor.
    A failure is part of this entry; it is not a later update. The entry is
    flagged unconfirmed, and a feed-on restore pauses later writes (decision 11).
    """
    values = _restore_values(
        snapshot_id, created_at, expired, rotated, ended, stopped, mfa_reset
    )
    with get_db_manager().session_scope() as session:
        if _trail_ready(session):
            payload = {
                trail.ACTION: trail.ACTION_RESTORE,
                trail.ACTOR: trail.SYSTEM,
                "abandoned": abandoned,
                **values,
            }
            if failure is not None:
                payload[trail.OUTCOME] = trail.OUTCOME_FAILED
                payload["reason"] = str(failure)
            # Poll's reset returns the newest entry with this action. Written
            # before the pause is set, so the gate never refuses it.
            entry = trail.append(session, payload, operator_text=actor)
            feed.pause_after_restore(session, entry.seq)
            return
    if not legacy:
        return
    get_config_service(user_id=actor).record_audit(
        config_type="backup",
        config_key="restore",
        action="restore",
        old_value=None,
        new_value=values,
    )


def _settle_restored(
    cfg: DatabaseConfig,
    manifest: dict,
    snapshot_id: str,
    actor: str,
    abandoned: dict | None,
    reapplied_at: datetime,
    paired_key: str | None,
    live_ring: dict,
) -> list[str]:
    created_at: str = manifest["created_at"]
    failure: BackupError | None = None
    expired = 0
    ended = stopped = mfa_reset = 0
    rotated = False
    new_secret = False
    from_env = False
    # True once settle has reached the point where today's audit row is written.
    # An earlier failure on a pre-trail snapshot still writes nothing.
    legacy = False
    try:
        _point_at_live(cfg)
        old_key = _stored_jwt_secret()
        # Under Compose the backend takes the key from the environment, which the
        # operator also passes to this process. No store holds it.
        env_key = os.environ.get("JWT_SECRET_KEY")  # noqa: ENV001
        from_env = old_key is None and bool(env_key)
        if from_env:
            old_key = env_key
        new_key = secrets.token_urlsafe(48)
        expired, new_secret, mfa_reset = _reencrypt_and_expire(
            old_key, new_key, snapshot_id, created_at, actor, paired_key, live_ring
        )
        # With no key anywhere, a deployment outside this install sets it. A
        # jwt_secret written here would never be read.
        if old_key is not None:
            if new_secret:
                _write_jwt_stores(new_key, secrets_only=from_env)
            rotated = True
        legacy = True
        if not rotated:
            failure = BackupError(
                f"approvals expired: {expired}\n"
                "JWT_SECRET_KEY is not in secrets.enc, either .env, or jwt_secret, "
                "so it is set outside this install; old sessions stay valid until "
                "it is rotated where it is defined"
            )
    except Exception as exc:
        # Any settle failure after the swap still records one entry. A
        # pre-trail snapshot only keeps today's row when settle reached it.
        failure = exc if isinstance(exc, BackupError) else BackupError(str(exc))
    # Apart from the key step: a failed key step still ends the sign-ins, and a
    # failed ending is still reported and recorded.
    try:
        ended, stopped = _end_restored_sessions(created_at, reapplied_at, mfa_reset)
    except Exception as exc:
        ending = BackupError(f"could not end the restored sign-ins: {exc}")
        failure = BackupError(f"{failure}\n{ending}") if failure else ending
    try:
        _record_restore(
            snapshot_id,
            created_at,
            expired,
            rotated,
            ended,
            stopped,
            mfa_reset,
            actor,
            abandoned,
            failure,
            legacy,
        )
    except BackupError:
        raise
    except Exception as exc:
        wrapped = BackupError(f"could not record the restore: {exc}")
        if failure is None:
            raise wrapped from exc
        raise BackupError(f"{failure}\n{wrapped}") from exc
    if failure is not None:
        raise failure
    lines = [
        f"backup date: {created_at}",
        f"approvals expired: {expired}",
        f"sign-ins ended: {ended}",
        f"runs stopped: {stopped}",
        "integration credentials and user accounts date from the backup",
    ]
    if mfa_reset:
        lines.insert(4, f"people who must set up their authenticator app: {mfa_reset}")
    if from_env and new_secret:
        lines.append(
            "the new JWT_SECRET_KEY is in "
            f"{EncryptedFileBackend(data_dir=vigil_path()).secrets_path}; clear or "
            "replace JWT_SECRET_KEY in the environment Compose reads before `up`, "
            "because an environment value outranks secrets.enc"
        )
    return lines


def _end_restored_sessions(
    created_at: str, reapplied_at: datetime, mfa_reset: int = 0
) -> tuple[int, int]:
    """End every open sign-in, stop every in-flight run, open the dormancy grace
    and tell the holders of ``sessions.manage`` once; returns (sign-ins ended,
    runs stopped).

    The runs stop through ``sign_in_runs._stop`` (the abort directive, the
    cancelled row, "stopped: sign-in ended"). This host process may have no
    workers or Redis, and ``stop_run``'s escalation timer dies with it, so each
    stopped run also gets its terminal in the ledger now.
    """
    from core.workflows.run_cancel import force_terminal

    runs = sign_in_runs.in_flight_runs()
    with unit_of_work() as session:
        ended = sign_ins.end_every_open(session, sign_ins.RESTORE)
        dormancy.start_grace(session)
        # People erased by this restore's own reapply were ended before this step.
        erased = (
            session.query(SignIn)
            .filter(
                SignIn.end_reason == sign_ins.ERASED, SignIn.ended_at >= reapplied_at
            )
            .count()
        )
    stopped = sign_in_runs._stop(runs)
    for run_id in stopped:
        force_terminal(run_id, sign_in_runs.STOP_REASON)
    _tell_session_holders(created_at, ended + erased, len(stopped), mfa_reset)
    return ended + erased, len(stopped)


def _tell_session_holders(
    created_at: str, ended: int, stopped: int, mfa_reset: int = 0
) -> None:
    """One notice to each active person who holds ``sessions.manage``. No names,
    no tokens."""
    from core.auth import permissions
    from core.platform.notices import notify

    until = dormancy.grace_until()
    # Prose, not a list: the bell shows a notice as one paragraph.
    message = (
        f"A restore from the backup of {created_at[:10]} has settled. "
        f"{ended} sign-in{'s' if ended != 1 else ''} ended, so everyone signs in "
        f"again, and {stopped} agent run{'s' if stopped != 1 else ''} stopped. "
        "Last-sign-in dates and failed-sign-in counts are as they were in the "
        "backup, so dormancy deactivates nobody and warns nobody until "
        f"{until:%Y-%m-%d} UTC. Locks set after the backup are gone."
    )
    if mfa_reset:
        message += (
            f" The backup's key ring could not be opened, so {mfa_reset} "
            f"{'person' if mfa_reset == 1 else 'people'} must set up the "
            "authenticator app again."
        )
    holders = permissions.holders("sessions.manage")
    for principal in holders:
        try:
            notify(
                principal,
                "restore_settled",
                "A restore ended every sign-in",
                message,
                link=SETTINGS_LINK,
                priority="high",
            )
        except Exception:  # noqa: BLE001 - the restore has already settled
            logger.exception("could not tell a session manager about the restore")


def _point_at_live(cfg: DatabaseConfig) -> None:
    # The process-wide manager may still point at another database. close()
    # does not clear that config.
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
    paired_key: str | None,
    live_ring: dict,
) -> tuple[int, bool, int]:
    """Expire pending approvals and sign everyone out; returns the count, whether
    JWT_SECRET_KEY must change (a database from before the key ring), and how many
    people must set up their authenticator app again.

    With a ring, the wrapping secret stays. The ring opens under it, or under the
    backup's paired wrapping key and is re-wrapped, or it is replaced (no old token
    verifies, and secrets no live MFA key opens are cleared). Versions retired
    before the restore stay retired. New sign-in and run-token keys are written
    with no overlap, so old sessions and run tokens stop verifying. Without a
    ring, as before it: the secret rotates and MFA secrets move to a key derived
    from the new one.
    """
    reason = f"expired: restored from backup {snapshot_id} taken {created_at}"
    decided_at = utcnow()
    legacy = False
    mfa_reset = 0
    try:
        with get_db_manager().session_scope() as session:
            if old_key and _ring_present(session):
                kept = key_ring.restore_ring(
                    session, old_key, paired_key, live_ring, operator=actor
                )
                if kept:  # a replaced ring has no old key left to rotate
                    for purpose in ("signin", "run"):
                        key_ring.rotate(
                            purpose,
                            session=session,
                            secret=old_key,
                            overlap=timedelta(0),
                            operator=actor,
                        )
                mfa_reset = key_ring.clear_unopenable_mfa(session, old_key)
            elif old_key:
                legacy = True
                _rewrap_mfa(session, old_key, new_key)
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
            return len(pending), legacy, mfa_reset
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(f"could not settle the restored database: {exc}") from exc


def _ring_present(session) -> bool:
    from core.storage.connection import table_present

    return table_present(session, "auth_keys") and (
        session.execute(sa_text("SELECT 1 FROM public.auth_keys LIMIT 1")).first()
        is not None
    )


def _rewrap_mfa(session, old_key: str, new_key: str) -> None:
    """Pre-ring databases: MFA secrets sit under a key derived from the secret."""
    derive = key_ring.PURPOSES["mfa"].derive
    old = Fernet(base64.urlsafe_b64encode(derive(old_key)))
    new = Fernet(base64.urlsafe_b64encode(derive(new_key)))
    users = session.query(User).filter(User.mfa_secret.isnot(None)).all()
    plaintext = []
    for user in users:
        try:
            plaintext.append(old.decrypt(user.mfa_secret.encode()).decode())
        except InvalidToken as exc:
            if not user.mfa_secret.startswith("gAAAAA"):
                plaintext.append(user.mfa_secret)  # legacy plaintext row
                continue
            # Nothing is written yet; the session rolls back.
            raise BackupError(
                "cannot rotate JWT_SECRET_KEY: the old key does not "
                f"decrypt the MFA secret of user {user.user_id}"
            ) from exc
    for user, secret in zip(users, plaintext):
        user.mfa_secret = new.encrypt(secret.encode()).decode()


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
