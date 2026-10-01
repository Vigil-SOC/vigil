"""``python -m core.backup create`` writes a verified restic snapshot."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from core.backup.create import LOCK_CLASSID, LOCK_OBJID, SKIPPED_MESSAGE
from core.version import __version__

pytestmark = [pytest.mark.integration, pytest.mark.database]

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT = REPO_ROOT / "infra" / "database" / "init"
SCRATCH_DB = "vigil_test_backup_create"


def _parts() -> dict[str, str]:
    return {
        "user": os.getenv("POSTGRES_USER", "deeptempo"),
        "password": os.getenv(
            "POSTGRES_PASSWORD", "deeptempo_secure_password_change_me"
        ),
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": os.getenv("POSTGRES_PORT", "5432"),
    }


def _url(database: str) -> str:
    part = _parts()
    return (
        f"postgresql://{part['user']}:{part['password']}"
        f"@{part['host']}:{part['port']}/{database}"
    )


def _postgres_available() -> bool:
    try:
        engine = create_engine(_url("postgres"), isolation_level="AUTOCOMMIT")
        with engine.connect():
            return True
    except Exception:
        return False


pytestmark.append(
    pytest.mark.skipif(
        not _postgres_available(),
        reason="requires a local PostgreSQL (docker compose up -d postgres)",
    )
)


@pytest.fixture(scope="module")
def scratch_db():
    admin = create_engine(_url("postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {SCRATCH_DB}"))
    owner = create_engine(_url(SCRATCH_DB))
    with owner.connect() as conn:
        conn.exec_driver_sql((INIT / "19_agent_ledger.sql").read_text())
        conn.execute(text("CREATE SCHEMA extra"))
        conn.execute(text("CREATE TABLE extra.widgets (id integer)"))
        conn.execute(text("INSERT INTO extra.widgets VALUES (1), (2)"))
        conn.execute(
            text("CREATE TABLE backup_sample (id integer primary key, note text)")
        )
        conn.execute(
            text(
                "INSERT INTO backup_sample (id, note) VALUES "
                "(1, 'a'), (2, 'b'), (3, 'c'), (4, 'd')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO agent_events "
                "(run_id, seq, run_kind, kind, payload, schema_version) "
                "VALUES (:run_id, 0, 'hunt', 'note', CAST(:payload AS jsonb), 1)"
            ),
            {
                "run_id": "11111111-1111-1111-1111-111111111111",
                "payload": json.dumps({"seed": "backup"}),
            },
        )
        conn.commit()
    yield
    owner.dispose()
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
    admin.dispose()


def _child_env(
    root: Path, *, skills: Path | None, workdir: Path, intent: Path
) -> dict[str, str]:
    part = _parts()
    env = os.environ.copy()
    env.update(
        {
            "POSTGRES_HOST": part["host"],
            "POSTGRES_PORT": part["port"],
            "POSTGRES_USER": part["user"],
            "POSTGRES_PASSWORD": part["password"],
            "POSTGRES_DB": SCRATCH_DB,
            "VIGIL_DIR": str(root / "state"),
            "ORCHESTRATOR_WORKDIR": str(workdir),
            "VIGIL_INTENT_PATH": str(intent),
            "VIGIL_DISABLE_DOTENV": "1",
            "RESTIC_CACHE_DIR": str(root / "cache"),
        }
    )
    if skills is None:
        env.pop("VIGIL_SKILLS_PATH", None)
    else:
        env["VIGIL_SKILLS_PATH"] = str(skills)
    return env


def _run_create(
    env: dict[str, str],
    repo: Path,
    passphrase: Path,
    *extra: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "core.backup",
            "create",
            "--repo",
            str(repo),
            "--passphrase-file",
            str(passphrase),
            *extra,
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
    )


def _restic_env(env: dict[str, str], passphrase: Path) -> dict[str, str]:
    out = env.copy()
    out["RESTIC_PASSWORD_FILE"] = str(passphrase)
    return out


def _listing(env: dict[str, str], repo: Path, passphrase: Path, snap: str) -> str:
    return subprocess.check_output(
        ["restic", "-r", str(repo), "ls", snap],
        env=_restic_env(env, passphrase),
        text=True,
    )


def _dump_json(env: dict[str, str], repo: Path, passphrase: Path, snap: str, path: str):
    raw = subprocess.check_output(
        ["restic", "-r", str(repo), "dump", snap, path],
        env=_restic_env(env, passphrase),
    )
    return json.loads(raw)


def _live_counts() -> dict[str, int]:
    engine = create_engine(_url(SCRATCH_DB))
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        rows = conn.execute(text("""
                SELECT n.nspname, c.relname
                FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind = 'r'
                  AND n.nspname <> 'pg_catalog'
                  AND n.nspname <> 'information_schema'
                  AND n.nspname NOT LIKE 'pg_toast%'
                ORDER BY 1, 2
                """))
        for schema, table in rows:
            quoted = f'"{schema}"."{table}"'
            counts[f"{schema}.{table}"] = int(
                conn.execute(text(f"SELECT count(*) FROM {quoted}")).scalar_one()
            )
    engine.dispose()
    return counts


def _assert_not_in_repo(repo: Path, needle: bytes) -> None:
    for path in repo.rglob("*"):
        if path.is_file() and needle in path.read_bytes():
            raise AssertionError(f"{needle!r} appeared in {path}")


def _passphrase(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _bifrost(path: Path, secret: str) -> sqlite3.Connection:
    """Open config.db in WAL mode and leave the connection open.

    The returned connection keeps ``config.db-wal`` on disk so the copy step
    has a live sidecar to leave behind.
    """
    path.mkdir()
    live = sqlite3.connect(path / "config.db")
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("CREATE TABLE item (v text)")
    live.execute("INSERT INTO item VALUES ('kept')")
    live.commit()
    logs = sqlite3.connect(path / "logs.db")
    logs.execute("CREATE TABLE t (v text)")
    logs.execute("INSERT INTO t VALUES (?)", (secret,))
    logs.commit()
    logs.close()
    (path / "note.txt").write_text("hello-bifrost", encoding="utf-8")
    assert (path / "config.db-wal").is_file()
    return live


def test_create_snapshot_lists_state_and_matches_counts(scratch_db, tmp_path: Path):
    root = tmp_path / "run"
    state = root / "state"
    (state / "nested").mkdir(parents=True)
    sentinel = f"VIGIL-BACKUP-SENTINEL-{uuid.uuid4().hex}"
    (state / "nested" / "keep.txt").write_text(sentinel, encoding="utf-8")
    workdir = root / "work"
    (workdir / "case").mkdir(parents=True)
    (workdir / "case" / "plan.md").write_text("plan", encoding="utf-8")
    skills = root / "skills"
    skills.mkdir()
    (skills / "SKILL.md").write_text("skill", encoding="utf-8")
    intent = root / "INTENT.md"
    intent.write_text("intent", encoding="utf-8")
    bifrost = root / "bifrost"
    log_secret = f"BIFROST-LOG-SECRET-{uuid.uuid4().hex}"
    live = _bifrost(bifrost, log_secret)
    repo = root / "repo"
    passphrase = _passphrase(root / "pass", "correct-horse")
    env = _child_env(root, skills=skills, workdir=workdir, intent=intent)
    (root / "cache").mkdir()

    try:
        proc = _run_create(env, repo, passphrase, "--bifrost-data", str(bifrost))
    finally:
        live.close()
    assert proc.returncode == 0, proc.stderr
    snap = proc.stdout.strip()
    assert len(snap) == 64

    listing = _listing(env, repo, passphrase, snap)
    manifest_path = next(
        line for line in listing.splitlines() if line.endswith("/manifest.json")
    )
    manifest = _dump_json(env, repo, passphrase, snap, manifest_path)
    assert manifest["version"] == __version__
    assert manifest["kind"] == "manual"
    datetime.fromisoformat(manifest["created_at"])
    assert manifest["tables"] == _live_counts()
    assert manifest["tables"]["public.agent_events"] == 1
    assert manifest["tables"]["public.backup_sample"] == 4
    assert manifest["tables"]["extra.widgets"] == 2
    by_name = {item["name"]: item for item in manifest["locations"]}
    for name in (
        "database",
        "state_directory",
        "orchestrator_workdir",
        "skills",
        "intent",
        "bifrost",
    ):
        assert by_name[name]["status"] == "included", by_name[name]
    env_status = "included" if (REPO_ROOT / ".env").is_file() else "absent"
    if env_status == "included":
        assert by_name["env"]["status"] == "included"
    else:
        assert by_name["env"] == {
            "name": "env",
            "status": "skipped",
            "reason": "absent",
        }

    assert "keep.txt" in listing
    assert "plan.md" in listing
    assert "SKILL.md" in listing
    assert listing.count("db.dump") >= 1
    assert "logs.db" not in listing
    assert ".db-wal" not in listing
    assert ".db-shm" not in listing
    config_db = next(
        line for line in listing.splitlines() if line.endswith("/bifrost/config.db")
    )
    blob = subprocess.check_output(
        ["restic", "-r", str(repo), "dump", snap, config_db],
        env=_restic_env(env, passphrase),
    )
    copy = root / "config-copy.db"
    copy.write_bytes(blob)
    assert sqlite3.connect(copy).execute("SELECT v FROM item").fetchone()[0] == "kept"
    note = next(
        line for line in listing.splitlines() if line.endswith("/bifrost/note.txt")
    )
    assert (
        subprocess.check_output(
            ["restic", "-r", str(repo), "dump", snap, note],
            env=_restic_env(env, passphrase),
        )
        == b"hello-bifrost"
    )
    _assert_not_in_repo(repo, sentinel.encode())
    _assert_not_in_repo(repo, log_secret.encode())
    assert (repo / "config").is_file()


def test_missing_locations_are_skipped(scratch_db, tmp_path: Path):
    root = tmp_path / "skip"
    (root / "state").mkdir(parents=True)
    (root / "state" / "only.txt").write_text("x", encoding="utf-8")
    (root / "cache").mkdir()
    repo = root / "repo"
    passphrase = _passphrase(root / "pass", "correct-horse")
    env = _child_env(
        root,
        skills=None,
        workdir=root / "missing-work",
        intent=root / "missing-intent.md",
    )
    proc = _run_create(env, repo, passphrase)
    assert proc.returncode == 0, proc.stderr
    snap = proc.stdout.strip()
    listing = _listing(env, repo, passphrase, snap)
    manifest_path = next(
        line for line in listing.splitlines() if line.endswith("/manifest.json")
    )
    manifest = _dump_json(env, repo, passphrase, snap, manifest_path)
    by_name = {item["name"]: item for item in manifest["locations"]}
    assert by_name["database"]["status"] == "included"
    assert by_name["state_directory"]["status"] == "included"
    assert by_name["orchestrator_workdir"]["reason"] == "absent"
    assert by_name["skills"]["reason"] == "unset"
    assert by_name["intent"]["reason"] == "absent"
    assert by_name["bifrost"]["reason"] == "unset"
    assert "only.txt" in listing


def test_wrong_passphrase_does_not_dump(scratch_db, tmp_path: Path):
    root = tmp_path / "pw"
    (root / "state").mkdir(parents=True)
    (root / "cache").mkdir()
    repo = root / "repo"
    good = _passphrase(root / "good", "correct-horse")
    env = _child_env(root, skills=None, workdir=root / "no-work", intent=root / "no.md")
    first = _run_create(env, repo, good)
    assert first.returncode == 0, first.stderr
    before = subprocess.check_output(
        ["restic", "-r", str(repo), "snapshots", "--json"],
        env=_restic_env(env, good),
        text=True,
    )

    real = shutil.which("pg_dump")
    assert real
    bindir = root / "bin"
    bindir.mkdir()
    marker = root / "pg_dump.invocations"
    wrapper = bindir / "pg_dump"
    wrapper.write_text(
        "#!/bin/sh\n" f"printf '%s\\n' \"$0 $*\" >> {marker}\n" f'exec {real} "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    bad_env = env.copy()
    bad_env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    bad = _passphrase(root / "bad", "wrong-passphrase")
    second = _run_create(bad_env, repo, bad)
    assert second.returncode != 0
    assert second.stdout == ""
    assert "wrong password" in second.stderr.lower()
    after = subprocess.check_output(
        ["restic", "-r", str(repo), "snapshots", "--json"],
        env=_restic_env(env, good),
        text=True,
    )
    assert after == before
    invocations = marker.read_text(encoding="utf-8") if marker.exists() else ""
    assert "--snapshot" not in invocations
    assert "--format" not in invocations


def test_large_dump_still_verifies(scratch_db, tmp_path: Path):
    """An incompressible dump still verifies and leaves the repository unlocked."""
    engine = create_engine(_url(SCRATCH_DB))
    payload = os.urandom(2 * 1024 * 1024)
    with engine.connect() as conn:
        conn.execute(text("DROP TABLE IF EXISTS backup_blob"))
        conn.execute(text("CREATE TABLE backup_blob (payload bytea)"))
        conn.execute(
            text("INSERT INTO backup_blob (payload) VALUES (:payload)"),
            {"payload": payload},
        )
        conn.commit()
    engine.dispose()

    root = tmp_path / "large"
    (root / "state").mkdir(parents=True)
    (root / "cache").mkdir()
    repo = root / "repo"
    passphrase = _passphrase(root / "pass", "correct-horse")
    env = _child_env(root, skills=None, workdir=root / "no-work", intent=root / "no.md")
    proc = _run_create(env, repo, passphrase)
    assert proc.returncode == 0, proc.stderr
    assert len(proc.stdout.strip()) == 64
    subprocess.check_call(
        ["restic", "-r", str(repo), "check"],
        env=_restic_env(env, passphrase),
        stdout=subprocess.DEVNULL,
    )


def test_corrupt_pack_fails_verification(scratch_db, tmp_path: Path):
    root = tmp_path / "corrupt"
    (root / "state").mkdir(parents=True)
    (root / "state" / "file.txt").write_text("data", encoding="utf-8")
    (root / "cache").mkdir()
    repo = root / "repo"
    passphrase = _passphrase(root / "pass", "correct-horse")
    env = _child_env(root, skills=None, workdir=root / "no-work", intent=root / "no.md")
    first = _run_create(env, repo, passphrase)
    assert first.returncode == 0, first.stderr
    packs = [path for path in (repo / "data").rglob("*") if path.is_file()]
    assert packs
    target = max(packs, key=lambda path: path.stat().st_size)
    target.chmod(0o644)
    blob = bytearray(target.read_bytes())
    # Shorten the pack. A flipped byte in the middle is ciphertext: restic
    # check does not read it, and the next backup may not reuse that blob.
    target.write_bytes(blob[:-1])

    second = _run_create(env, repo, passphrase)
    assert second.returncode != 0
    assert "verification failed" in second.stderr


def test_concurrent_create_skips(scratch_db, tmp_path: Path):
    root = tmp_path / "lock"
    state = root / "state"
    state.mkdir(parents=True)
    (state / "blob.bin").write_bytes(os.urandom(8 * 1024 * 1024))
    (root / "cache").mkdir()
    repo = root / "repo"
    passphrase = _passphrase(root / "pass", "correct-horse")
    env = _child_env(root, skills=None, workdir=root / "no-work", intent=root / "no.md")
    cmd = [
        sys.executable,
        "-m",
        "core.backup",
        "create",
        "--repo",
        str(repo),
        "--passphrase-file",
        str(passphrase),
    ]
    first = subprocess.Popen(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    seen = False
    deadline = time.time() + 60
    while time.time() < deadline:
        if _lock_held():
            seen = True
            break
        if first.poll() is not None:
            break
        time.sleep(0.02)
    assert seen, "the first create finished before its lock was visible"
    second = _run_create(env, repo, passphrase)
    out, err = first.communicate(timeout=180)
    assert second.returncode != 0
    assert second.stderr.strip() == SKIPPED_MESSAGE
    assert first.returncode == 0, err
    assert out.strip()
    subprocess.check_call(
        ["restic", "-r", str(repo), "check"],
        env=_restic_env(env, passphrase),
        stdout=subprocess.DEVNULL,
    )


def test_database_failure_writes_no_repository(scratch_db, tmp_path: Path):
    root = tmp_path / "down"
    (root / "state").mkdir(parents=True)
    (root / "cache").mkdir()
    repo = root / "repo"
    passphrase = _passphrase(root / "pass", "correct-horse")
    env = _child_env(root, skills=None, workdir=root / "no-work", intent=root / "no.md")
    env["POSTGRES_DB"] = "vigil_backup_missing_database"
    proc = _run_create(env, repo, passphrase)
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert not (repo / "config").exists()


def _lock_held() -> bool:
    engine = create_engine(_url(SCRATCH_DB))
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT 1 FROM pg_locks "
                "WHERE locktype = 'advisory' AND classid = :classid "
                "AND objid = :objid AND granted"
            ),
            {"classid": LOCK_CLASSID, "objid": LOCK_OBJID},
        ).first()
    engine.dispose()
    return row is not None
