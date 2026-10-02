"""``create`` and ``restore --test`` against a restic repository on MinIO."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text

pytestmark = [pytest.mark.integration, pytest.mark.database]

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT = REPO_ROOT / "infra" / "database" / "init"
SCRATCH_DB = "vigil_s3_backup"
MINIO_ENDPOINT = "http://127.0.0.1:9000"
MINIO_BUCKET = "vigil-backup"
MINIO_ACCESS_KEY = "vigilbackup"
MINIO_SECRET_KEY = "vigilbackupsecret"
RUN_ID = "11111111-1111-1111-1111-111111111111"


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


def _minio_listening() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 9000), timeout=2):
            return True
    except OSError:
        return False


pytestmark.append(
    pytest.mark.skipif(
        not _postgres_available(),
        reason="requires a local PostgreSQL (docker compose up -d postgres)",
    )
)
pytestmark.append(
    pytest.mark.skipif(
        not os.environ.get("CI") and not _minio_listening(),
        reason="MinIO is not listening on 127.0.0.1:9000",
    )
)


@pytest.fixture
def scratch_db():
    admin = create_engine(_url("postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {SCRATCH_DB}"))
    owner = create_engine(_url(SCRATCH_DB))
    with owner.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        conn.exec_driver_sql((INIT / "19_agent_ledger.sql").read_text())
        conn.exec_driver_sql((INIT / "31_agent_ledger_hash_chain.sql").read_text())
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
            {"run_id": RUN_ID, "payload": json.dumps({"seed": "backup"})},
        )
        conn.commit()
    yield
    owner.dispose()
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
    admin.dispose()


def _child_env(root: Path, paths: dict[str, Path]) -> dict[str, str]:
    part = _parts()
    env = os.environ.copy()
    env.update(
        {
            "POSTGRES_HOST": part["host"],
            "POSTGRES_PORT": part["port"],
            "POSTGRES_USER": part["user"],
            "POSTGRES_PASSWORD": part["password"],
            "POSTGRES_DB": SCRATCH_DB,
            "VIGIL_DIR": str(paths["state"]),
            "ORCHESTRATOR_WORKDIR": str(paths["workdir"]),
            "VIGIL_SKILLS_PATH": str(paths["skills"]),
            "VIGIL_INTENT_PATH": str(paths["intent"]),
            "VIGIL_DISABLE_DOTENV": "1",
            "RESTIC_CACHE_DIR": str(root / "cache"),
            "JWT_SECRET_KEY": env.get("JWT_SECRET_KEY")
            or "test-only-secret-not-for-prod",
            "AWS_ACCESS_KEY_ID": MINIO_ACCESS_KEY,
            "AWS_SECRET_ACCESS_KEY": MINIO_SECRET_KEY,
        }
    )
    return env


def _layout(root: Path) -> dict[str, Path]:
    state = root / "state"
    (state / "nested").mkdir(parents=True)
    (state / "nested" / "keep.txt").write_text("sentinel", encoding="utf-8")
    key = Fernet.generate_key()
    (state / "master.key").write_bytes(key)
    (state / "secrets.enc").write_bytes(
        Fernet(key).encrypt(json.dumps({"token": "sekrit"}).encode())
    )
    workdir = root / "work"
    (workdir / "case").mkdir(parents=True)
    (workdir / "case" / "plan.md").write_text("plan", encoding="utf-8")
    skills = root / "skills"
    skills.mkdir()
    (skills / "SKILL.md").write_text("skill", encoding="utf-8")
    intent = root / "INTENT.md"
    intent.write_text("intent", encoding="utf-8")
    (root / "cache").mkdir()
    return {"state": state, "workdir": workdir, "skills": skills, "intent": intent}


def _run(cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=REPO_ROOT, env=env, text=True, capture_output=True)


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


def _restic_env(env: dict[str, str], passphrase: Path) -> dict[str, str]:
    out = env.copy()
    out["RESTIC_PASSWORD_FILE"] = str(passphrase)
    return out


def test_s3_snapshot_matches_counts_and_rejects_wrong_passphrase(
    scratch_db, tmp_path: Path
):
    if os.environ.get("CI") and not _minio_listening():
        pytest.fail("MinIO is not listening on 127.0.0.1:9000")

    root = tmp_path / "s3"
    paths = _layout(root)
    passphrase = root / "pass"
    passphrase.write_text("correct-horse", encoding="utf-8")
    repo = f"s3:{MINIO_ENDPOINT}/{MINIO_BUCKET}/{uuid.uuid4().hex}"
    env = _child_env(root, paths)

    created = _run(
        [
            sys.executable,
            "-m",
            "core.backup",
            "create",
            "--repo",
            repo,
            "--passphrase-file",
            str(passphrase),
        ],
        env,
    )
    assert created.returncode == 0, created.stderr
    snap = created.stdout.strip()
    assert len(snap) == 64

    listing = subprocess.check_output(
        ["restic", "-r", repo, "ls", snap],
        env=_restic_env(env, passphrase),
        text=True,
    )
    manifest_path = next(
        line for line in listing.splitlines() if line.endswith("/manifest.json")
    )
    manifest = json.loads(
        subprocess.check_output(
            ["restic", "-r", repo, "dump", snap, manifest_path],
            env=_restic_env(env, passphrase),
        )
    )
    assert manifest["tables"] == _live_counts()

    restored = _run(
        [
            sys.executable,
            "-m",
            "core.backup",
            "restore",
            "--repo",
            repo,
            "--passphrase-file",
            str(passphrase),
            "--test",
        ],
        env,
    )
    assert restored.returncode == 0, restored.stderr
    for needle in ("rows: ok", "ledger: ok", "schema: ok", "secrets: ok"):
        assert needle in restored.stdout

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
    bad = root / "bad"
    bad.write_text("wrong-passphrase", encoding="utf-8")
    second = _run(
        [
            sys.executable,
            "-m",
            "core.backup",
            "create",
            "--repo",
            repo,
            "--passphrase-file",
            str(bad),
        ],
        bad_env,
    )
    assert second.returncode != 0
    assert second.stdout == ""
    assert "wrong password" in second.stderr.lower()
    invocations = marker.read_text(encoding="utf-8") if marker.exists() else ""
    assert "--snapshot" not in invocations
    assert "--format" not in invocations
