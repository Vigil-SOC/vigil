"""``python -m core.backup restore`` stages a snapshot, checks it, and swaps."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import psycopg2
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text

from core.config import REPO_ROOT as CORE_REPO_ROOT

pytestmark = [pytest.mark.integration, pytest.mark.database]

REPO_ROOT = Path(__file__).resolve().parents[2]
INIT = REPO_ROOT / "infra" / "database" / "init"
RUN_ID = "11111111-1111-1111-1111-111111111111"

_SERVE = """
import sys
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api.v1.agent_runs_router import router
from core.storage.connection import init_database

init_database()
app = FastAPI()
app.include_router(router, prefix="/api/v1/agent-runs")
response = TestClient(app).get("/api/v1/agent-runs/" + sys.argv[1])
print(response.status_code)
print(response.text)
"""


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


@pytest.fixture(autouse=True)
def _preserve_repo_env():
    path = CORE_REPO_ROOT / ".env"
    existed = path.is_file()
    original = path.read_bytes() if existed else None
    mode = path.stat().st_mode if existed else None
    yield
    for leftover in list(CORE_REPO_ROOT.glob(".env_pre_restore_*")):
        leftover.unlink()
    for leftover in list(CORE_REPO_ROOT.glob(".env.vigil-restore-stage-*")):
        if leftover.is_dir() and not leftover.is_symlink():
            shutil.rmtree(leftover)
        else:
            leftover.unlink()
    if existed:
        path.write_bytes(original)
        path.chmod(mode)
    elif path.exists() or path.is_symlink():
        path.unlink()


@pytest.fixture
def scratch_databases():
    _drop_matching()
    yield
    _drop_matching()


def _drop_matching() -> None:
    admin = create_engine(_url("postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        names = [
            row[0]
            for row in conn.execute(
                text(
                    "SELECT datname FROM pg_database WHERE datname LIKE 'vigil_r\\_%%'"
                )
            )
        ]
        for name in names:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def _create_database(name: str, *, ledger: bool) -> None:
    admin = create_engine(_url("postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    if not ledger:
        return
    owner = create_engine(_url(name))
    with owner.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        conn.exec_driver_sql((INIT / "19_agent_ledger.sql").read_text())
        conn.exec_driver_sql((INIT / "31_agent_ledger_hash_chain.sql").read_text())
        conn.execute(
            text("CREATE TABLE backup_sample (id integer primary key, note text)")
        )
        conn.execute(text("INSERT INTO backup_sample (id, note) VALUES (1, 'kept')"))
        conn.execute(
            text(
                "INSERT INTO agent_events "
                "(run_id, seq, run_kind, kind, payload, schema_version) "
                "VALUES (:run_id, 0, 'hunt', 'note', CAST(:payload AS jsonb), 1)"
            ),
            {"run_id": RUN_ID, "payload": json.dumps({"seed": "backup"})},
        )
        conn.commit()
    owner.dispose()


def _child_env(
    root: Path, database: str, *, state: Path, workdir: Path, skills: Path, intent: Path
) -> dict[str, str]:
    part = _parts()
    env = os.environ.copy()
    env.update(
        {
            "POSTGRES_HOST": part["host"],
            "POSTGRES_PORT": part["port"],
            "POSTGRES_USER": part["user"],
            "POSTGRES_PASSWORD": part["password"],
            "POSTGRES_DB": database,
            "VIGIL_DIR": str(state),
            "ORCHESTRATOR_WORKDIR": str(workdir),
            "VIGIL_SKILLS_PATH": str(skills),
            "VIGIL_INTENT_PATH": str(intent),
            "VIGIL_DISABLE_DOTENV": "1",
            "RESTIC_CACHE_DIR": str(root / "cache"),
            "JWT_SECRET_KEY": env.get("JWT_SECRET_KEY")
            or "test-only-secret-not-for-prod",
        }
    )
    return env


def _passphrase(path: Path) -> Path:
    path.write_text("correct-horse", encoding="utf-8")
    return path


def _layout(root: Path, *, secret: str | None) -> dict[str, Path]:
    state = root / "state"
    (state / "nested").mkdir(parents=True)
    (state / "nested" / "keep.txt").write_text("sentinel", encoding="utf-8")
    if secret is not None:
        key = Fernet.generate_key()
        (state / "master.key").write_bytes(key)
        (state / "secrets.enc").write_bytes(
            Fernet(key).encrypt(json.dumps({"token": secret}).encode())
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


def _empty_layout(root: Path) -> dict[str, Path]:
    paths = {
        "state": root / "state",
        "workdir": root / "work",
        "skills": root / "skills",
    }
    for path in paths.values():
        path.mkdir(parents=True)
    intent = root / "INTENT.md"
    intent.write_text("", encoding="utf-8")
    paths["intent"] = intent
    (root / "cache").mkdir()
    return paths


def _run(cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=REPO_ROOT, env=env, text=True, capture_output=True)


def _create(env: dict[str, str], repo: Path, passphrase: Path, *extra: str):
    return _run(
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
        env,
    )


def _restore(env: dict[str, str], repo: Path, passphrase: Path, *extra: str):
    return _run(
        [
            sys.executable,
            "-m",
            "core.backup",
            "restore",
            "--repo",
            str(repo),
            "--passphrase-file",
            str(passphrase),
            *extra,
        ],
        env,
    )


def _oid(database: str) -> int:
    engine = create_engine(_url("postgres"), isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        oid = conn.execute(
            text("SELECT oid FROM pg_database WHERE datname = :name"),
            {"name": database},
        ).scalar_one()
    engine.dispose()
    return int(oid)


def _payloads(database: str) -> list[tuple]:
    engine = create_engine(_url(database))
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT seq, payload::text, event_hash FROM agent_events ORDER BY seq")
        ).fetchall()
    engine.dispose()
    return [tuple(row) for row in rows]


def _files(path: Path) -> list[tuple[str, int, bytes]]:
    if path.is_file():
        return [("", path.stat().st_ino, path.read_bytes())]
    found = []
    for item in sorted(path.rglob("*")):
        if item.is_file() and not item.is_symlink():
            found.append(
                (str(item.relative_to(path)), item.stat().st_ino, item.read_bytes())
            )
    return found


def _names() -> list[str]:
    engine = create_engine(_url("postgres"), isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT datname FROM pg_database WHERE datname LIKE 'vigil_r\\_%%'")
        ).fetchall()
    engine.dispose()
    return [row[0] for row in rows]


def test_round_trip_serves_restored_database(scratch_databases, tmp_path: Path):
    root = tmp_path / "round"
    root.mkdir()
    paths = _layout(root, secret="sekrit")
    bifrost = root / "bifrost"
    bifrost.mkdir()
    (bifrost / "note.txt").write_text("hello-bifrost", encoding="utf-8")
    _create_database("vigil_r_round", ledger=True)
    repo = root / "repo"
    passphrase = _passphrase(root / "pass")
    env = _child_env(root, "vigil_r_round", **paths)
    created = _create(env, repo, passphrase, "--bifrost-data", str(bifrost))
    assert created.returncode == 0, created.stderr
    source_oid = _oid("vigil_r_round")
    source_files = {name: _files(path) for name, path in paths.items()}

    dest_root = root / "dest"
    dest_root.mkdir()
    dest = _empty_layout(dest_root)
    _create_database("vigil_r_round_dst", ledger=False)
    dest_oid = _oid("vigil_r_round_dst")
    dest_env = _child_env(dest_root, "vigil_r_round_dst", **dest)
    dest_env["RESTIC_CACHE_DIR"] = str(root / "cache")
    restored = _restore(dest_env, repo, passphrase)
    assert restored.returncode == 0, restored.stderr
    assert "rows: ok" in restored.stdout
    assert "ledger: ok" in restored.stdout
    assert "schema: ok" in restored.stdout
    assert "secrets: ok" in restored.stdout
    previous = next(
        line.split(": ", 1)[1]
        for line in restored.stdout.splitlines()
        if line.startswith("previous database: ")
    )
    assert _oid(previous) == dest_oid
    assert _oid("vigil_r_round") == source_oid
    assert _payloads("vigil_r_round_dst")[0][1] == '{"seed": "backup"}'
    assert (dest["state"] / "nested" / "keep.txt").read_text() == "sentinel"
    assert (dest["workdir"] / "case" / "plan.md").read_text() == "plan"
    assert source_files["state"] == _files(paths["state"])
    key = (dest["state"] / "master.key").read_bytes()
    token = json.loads(
        Fernet(key).decrypt((dest["state"] / "secrets.enc").read_bytes())
    )
    assert token == {"token": "sekrit"}

    listing = subprocess.check_output(
        ["restic", "-r", str(repo), "ls", "latest"],
        env={**env, "RESTIC_PASSWORD_FILE": str(passphrase)},
        text=True,
    )
    manifest_path = next(
        line for line in listing.splitlines() if line.endswith("/manifest.json")
    )
    manifest = json.loads(
        subprocess.check_output(
            ["restic", "-r", str(repo), "dump", "latest", manifest_path],
            env={**env, "RESTIC_PASSWORD_FILE": str(passphrase)},
        )
    )
    bifrost_path = Path(
        next(
            item["path"] for item in manifest["locations"] if item["name"] == "bifrost"
        )
    )
    try:
        assert (bifrost_path / "note.txt").read_text(
            encoding="utf-8"
        ) == "hello-bifrost"
    finally:
        if bifrost_path.exists():
            shutil.rmtree(bifrost_path)
        if bifrost_path.parent.name.startswith("vigil-backup-"):
            shutil.rmtree(bifrost_path.parent, ignore_errors=True)

    served = subprocess.run(
        [sys.executable, "-c", _SERVE, RUN_ID],
        cwd=REPO_ROOT,
        env=dest_env,
        text=True,
        capture_output=True,
    )
    assert served.returncode == 0, served.stderr
    status, body = served.stdout.splitlines()
    assert status == "200"
    payload = json.loads(body)
    assert payload["status"] == "running"
    assert payload["events"] == 1
    assert payload["run_id"] == RUN_ID


def test_test_flag_leaves_live_byte_identical(scratch_databases, tmp_path: Path):
    root = tmp_path / "testflag"
    root.mkdir()
    paths = _layout(root, secret="sekrit")
    _create_database("vigil_r_test", ledger=True)
    repo = root / "repo"
    passphrase = _passphrase(root / "pass")
    env = _child_env(root, "vigil_r_test", **paths)
    created = _create(env, repo, passphrase)
    assert created.returncode == 0, created.stderr
    before_oid = _oid("vigil_r_test")
    before_rows = _payloads("vigil_r_test")
    before_files = {name: _files(path) for name, path in paths.items()}
    before_names = _names()

    proc = _restore(env, repo, passphrase, "--test")
    assert proc.returncode == 0, proc.stderr
    assert "discarded staged copies" in proc.stdout
    assert "previous database:" not in proc.stdout
    assert _oid("vigil_r_test") == before_oid
    assert _payloads("vigil_r_test") == before_rows
    assert {name: _files(path) for name, path in paths.items()} == before_files
    assert _names() == before_names


def test_other_major_minor_is_refused(scratch_databases, tmp_path: Path):
    root = tmp_path / "version"
    root.mkdir()
    paths = _layout(root, secret=None)
    _create_database("vigil_r_ver", ledger=True)
    repo = root / "repo"
    passphrase = _passphrase(root / "pass")
    env = _child_env(root, "vigil_r_ver", **paths)
    version_file = REPO_ROOT / "VERSION"
    original = version_file.read_text(encoding="utf-8")
    try:
        version_file.write_text("9.9.0\n", encoding="utf-8")
        created = _create(env, repo, passphrase)
    finally:
        version_file.write_text(original, encoding="utf-8")
    assert created.returncode == 0, created.stderr
    before = _names()
    proc = _restore(env, repo, passphrase)
    assert proc.returncode != 0
    assert "install 9.9.0" in proc.stderr
    assert proc.stdout == ""
    assert _names() == before


def test_broken_ledger_does_not_swap(scratch_databases, tmp_path: Path):
    root = tmp_path / "ledger"
    root.mkdir()
    paths = _layout(root, secret=None)
    _create_database("vigil_r_led", ledger=True)
    engine = create_engine(_url("vigil_r_led"))
    with engine.connect() as conn:
        conn.execute(text("UPDATE agent_events SET payload = '{\"tampered\": true}'"))
        conn.commit()
    engine.dispose()
    repo = root / "repo"
    passphrase = _passphrase(root / "pass")
    env = _child_env(root, "vigil_r_led", **paths)
    created = _create(env, repo, passphrase)
    assert created.returncode == 0, created.stderr

    dest_root = root / "dest"
    dest_root.mkdir()
    dest = _empty_layout(dest_root)
    _create_database("vigil_r_led_dst", ledger=False)
    dest_oid = _oid("vigil_r_led_dst")
    dest_files = {name: _files(path) for name, path in dest.items()}
    proc = _restore(_child_env(dest_root, "vigil_r_led_dst", **dest), repo, passphrase)
    assert proc.returncode != 0
    assert "ledger check failed" in proc.stderr
    assert "payload" in proc.stderr
    assert proc.stdout == ""
    assert _oid("vigil_r_led_dst") == dest_oid
    assert {name: _files(path) for name, path in dest.items()} == dest_files
    assert not any("pre_restore" in name for name in _names())


def test_open_connection_refuses_before_staging(scratch_databases, tmp_path: Path):
    root = tmp_path / "busy"
    root.mkdir()
    paths = _layout(root, secret=None)
    _create_database("vigil_r_busy", ledger=True)
    repo = root / "repo"
    passphrase = _passphrase(root / "pass")
    env = _child_env(root, "vigil_r_busy", **paths)
    created = _create(env, repo, passphrase)
    assert created.returncode == 0, created.stderr

    bindir = root / "bin"
    bindir.mkdir()
    log = root / "tools.log"
    for name in ("restic", "pg_restore"):
        real = shutil.which(name)
        assert real
        script = bindir / name
        script.write_text(
            "#!/bin/sh\n" f"printf '%s\\n' {name} >> {log}\n" f'exec {real} "$@"\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
    busy = env.copy()
    busy["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    part = _parts()
    held = psycopg2.connect(
        host=part["host"],
        port=part["port"],
        user=part["user"],
        password=part["password"],
        dbname="vigil_r_busy",
    )
    try:
        before_oid = _oid("vigil_r_busy")
        proc = _restore(busy, repo, passphrase)
    finally:
        held.close()
    assert proc.returncode != 0
    assert "other connection" in proc.stderr
    assert proc.stdout == ""
    assert _oid("vigil_r_busy") == before_oid
    assert not log.exists()


def test_undecryptable_secrets_do_not_swap(scratch_databases, tmp_path: Path):
    root = tmp_path / "secrets"
    root.mkdir()
    paths = _layout(root, secret=None)
    (paths["state"] / "master.key").write_bytes(Fernet.generate_key())
    (paths["state"] / "secrets.enc").write_bytes(b"not-a-secret")
    key_before = (paths["state"] / "master.key").read_bytes()
    _create_database("vigil_r_sec", ledger=True)
    repo = root / "repo"
    passphrase = _passphrase(root / "pass")
    env = _child_env(root, "vigil_r_sec", **paths)
    created = _create(env, repo, passphrase)
    assert created.returncode == 0, created.stderr

    dest_root = root / "dest"
    dest_root.mkdir()
    dest = _empty_layout(dest_root)
    _create_database("vigil_r_sec_dst", ledger=False)
    dest_oid = _oid("vigil_r_sec_dst")
    proc = _restore(_child_env(dest_root, "vigil_r_sec_dst", **dest), repo, passphrase)
    assert proc.returncode != 0
    assert "secrets check failed" in proc.stderr
    assert _oid("vigil_r_sec_dst") == dest_oid
    assert (paths["state"] / "master.key").read_bytes() == key_before
    assert not (dest["state"] / "master.key").exists()
    assert list(dest["state"].iterdir()) == []


def test_failed_location_removes_staging_siblings(scratch_databases, tmp_path: Path):
    root = tmp_path / "partial"
    root.mkdir()
    paths = _layout(root, secret=None)
    _create_database("vigil_r_part", ledger=True)
    repo = root / "repo"
    passphrase = _passphrase(root / "pass")
    created = _create(_child_env(root, "vigil_r_part", **paths), repo, passphrase)
    assert created.returncode == 0, created.stderr

    dest_root = root / "dest"
    dest_root.mkdir()
    dest = _empty_layout(dest_root)
    _create_database("vigil_r_part_dst", ledger=False)
    env = _child_env(dest_root, "vigil_r_part_dst", **dest)
    env.pop("VIGIL_SKILLS_PATH")
    before = {name: _files(path) for name, path in dest.items()}
    proc = _restore(env, repo, passphrase)
    assert proc.returncode != 0
    assert "VIGIL_SKILLS_PATH is unset" in proc.stderr
    assert {name: _files(path) for name, path in dest.items()} == before
    staged = list(dest_root.rglob("*.vigil-restore-stage-*"))
    assert staged == []
