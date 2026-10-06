"""``python -m core.backup restore`` stages a snapshot, checks it, and swaps."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import jwt
import psycopg2
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text

import core.storage.connection  # noqa: F401  registers models on Base.metadata
from core.config import REPO_ROOT as CORE_REPO_ROOT
from core.storage.models.base import Base

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
        f"postgresql+psycopg2://{part['user']}:{part['password']}"
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


def _layout(
    root: Path, *, secret: str | None, signing_key: bool = True
) -> dict[str, Path]:
    state = root / "state"
    (state / "nested").mkdir(parents=True)
    (state / "nested" / "keep.txt").write_text("sentinel", encoding="utf-8")
    if signing_key:
        # The file start.sh mints when JWT_SECRET_KEY is unset.
        (state / "jwt_secret").write_text("layout-signing-key", encoding="utf-8")
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
    dest_bifrost = dest_root / "bifrost"
    dest_bifrost.mkdir()
    (dest_bifrost / "old.txt").write_text("old-bifrost", encoding="utf-8")
    (dest["state"] / "old-state.txt").write_text("old-state", encoding="utf-8")
    targets = {**dest, "bifrost": dest_bifrost}
    inodes = {
        name: os.stat(path).st_ino for name, path in targets.items() if path.is_dir()
    }
    restored = _restore(dest_env, repo, passphrase, "--bifrost-data", str(dest_bifrost))
    assert restored.returncode == 0, restored.stderr
    # Directories are emptied and refilled, never renamed (mount points under Compose).
    assert inodes == {
        name: os.stat(path).st_ino for name, path in targets.items() if path.is_dir()
    }
    assert (dest_bifrost / "note.txt").read_text(encoding="utf-8") == "hello-bifrost"
    assert not (dest_bifrost / "old.txt").exists()
    (bifrost_pre,) = dest_bifrost.glob(".vigil-pre-restore-*")
    assert (bifrost_pre / "old.txt").read_text(encoding="utf-8") == "old-bifrost"
    assert f"previous bifrost: {bifrost_pre}" in restored.stdout
    (state_pre,) = dest["state"].glob(".vigil-pre-restore-*")
    assert (state_pre / "old-state.txt").read_text(encoding="utf-8") == "old-state"
    assert (state_pre / "intent").read_text(encoding="utf-8") == ""
    assert not list(dest_root.rglob(".vigil-restore-stage-*"))
    assert dest["intent"].read_text(encoding="utf-8") == "intent"
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

    # The next snapshot must not carry the previous copies, old master.key included.
    again = _create(dest_env, repo, passphrase, "--bifrost-data", str(dest_bifrost))
    assert again.returncode == 0, again.stderr
    listing = subprocess.check_output(
        ["restic", "-r", str(repo), "ls", "latest"],
        env={**dest_env, "RESTIC_PASSWORD_FILE": str(passphrase)},
        text=True,
    )
    assert ".vigil-pre-restore-" not in listing
    assert ".vigil-restore-stage-" not in listing
    assert "/state/nested/keep.txt" in listing
    assert "note.txt" in listing

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
    assert "backup date:" not in proc.stdout
    assert "approvals expired:" not in proc.stdout
    assert "integration credentials and user accounts" not in proc.stdout
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
    assert list(dest_root.rglob(".vigil-*")) == []


_CHECK_TOKENS = """
import os
import sys

os.environ.pop("JWT_SECRET_KEY", None)
from core.auth.auth_service import AuthService

old, new, mfa = sys.argv[1:]
print("reject_old", AuthService.verify_jwt_token(old) is None)
print("accept_new", (AuthService.verify_jwt_token(new) or {}).get("sub"))
print("mfa", AuthService._decrypt_mfa_secret(mfa))
"""

_OLD_KEY = "old-jwt-secret-from-store"
_STALE_ENV_KEY = "stale-process-environment-secret"
_TOTP = "JBSWY3DPEHPK3PXP"


def _mfa_ciphertext(key: str, secret: str) -> str:
    fernet_key = base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest())
    return Fernet(fernet_key).encrypt(secret.encode()).decode()


def _rows(database: str, statement: str) -> list[tuple]:
    engine = create_engine(_url(database))
    with engine.connect() as conn:
        found = conn.execute(text(statement)).fetchall()
    engine.dispose()
    return [tuple(row) for row in found]


def _install_app_rows(database: str, mfa: str) -> None:
    engine = create_engine(_url(database))
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO roles "
                "(role_id, name, description, permissions, is_system_role) "
                "VALUES ('role-analyst', 'Analyst', 'analyst', '{}', true)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO users "
                "(user_id, username, email, password_hash, full_name, role_id, "
                "is_active, is_verified, mfa_enabled, mfa_secret, mfa_recovery_codes, "
                "login_count) VALUES "
                "('u-mfa', 'ada', 'ada@example.com', 'hash', 'Ada', 'role-analyst', "
                "true, true, true, :mfa, '[]', 0), "
                "('u-none', 'bea', 'bea@example.com', 'hash', 'Bea', 'role-analyst', "
                "true, true, false, NULL, '[]', 0)"
            ),
            {"mfa": mfa},
        )
        conn.execute(
            text(
                "INSERT INTO workflow_runs "
                "(run_id, workflow_id, workflow_name, status) "
                "VALUES ('run-keep', 'wf', 'keep', 'paused')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO approval_actions "
                "(action_id, action_type, title, description, target, reason, "
                "created_by, status, workflow_run_id) VALUES "
                "('pend-plain', 'custom', 't', 'd', 'x', 'because', 'agent', "
                "'pending', NULL), "
                "('pend-run', 'custom', 't', 'd', 'x', 'because', 'agent', "
                "'pending', 'run-keep'), "
                "('keep-approved', 'custom', 't', 'd', 'x', 'because', 'agent', "
                "'approved', NULL), "
                "('keep-executed', 'custom', 't', 'd', 'x', 'because', 'agent', "
                "'executed', NULL), "
                "('keep-failed', 'custom', 't', 'd', 'x', 'because', 'agent', "
                "'failed', NULL)"
            )
        )
    engine.dispose()


def test_restore_rotates_jwt_and_expires_pending_approvals(
    scratch_databases, tmp_path: Path
):
    root = tmp_path / "settle"
    root.mkdir()
    paths = _layout(root, secret="sekrit")
    key = (paths["state"] / "master.key").read_bytes()
    (paths["state"] / "secrets.enc").write_bytes(
        Fernet(key).encrypt(
            json.dumps({"token": "sekrit", "JWT_SECRET_KEY": _OLD_KEY}).encode()
        )
    )
    (paths["state"] / "jwt_secret").write_text(_OLD_KEY, encoding="utf-8")
    (paths["state"] / ".env").write_text(
        'KEEP_STATE=yes\nJWT_SECRET_KEY="not-the-key"\n'
        f'JWT_SECRET_KEY="{_OLD_KEY}"\n',
        encoding="utf-8",
    )
    env_path = CORE_REPO_ROOT / ".env"
    env_path.write_text(
        "# sentinel-keep\nOTHER_KEY=leave-me\n"
        f"JWT_SECRET_KEY=not-the-key\nJWT_SECRET_KEY={_OLD_KEY}\n",
        encoding="utf-8",
    )
    _create_database("vigil_r_settle", ledger=True)
    ciphertext = _mfa_ciphertext(_OLD_KEY, _TOTP)
    _install_app_rows("vigil_r_settle", ciphertext)
    repo = root / "repo"
    passphrase = _passphrase(root / "pass")
    env = _child_env(root, "vigil_r_settle", **paths)
    env["JWT_SECRET_KEY"] = _STALE_ENV_KEY
    created = _create(env, repo, passphrase)
    assert created.returncode == 0, created.stderr

    before_files = {name: _files(path) for name, path in paths.items()}
    before_env = env_path.read_bytes()
    before_rows = _rows(
        "vigil_r_settle",
        "SELECT action_id, status, rejection_reason FROM approval_actions "
        "ORDER BY action_id",
    )
    probed = _restore(env, repo, passphrase, "--test")
    assert probed.returncode == 0, probed.stderr
    assert "approvals expired:" not in probed.stdout
    assert "backup date:" not in probed.stdout
    assert "integration credentials and user accounts" not in probed.stdout
    assert {name: _files(path) for name, path in paths.items()} == before_files
    assert env_path.read_bytes() == before_env
    assert (
        _rows(
            "vigil_r_settle",
            "SELECT action_id, status, rejection_reason FROM approval_actions "
            "ORDER BY action_id",
        )
        == before_rows
    )
    assert _rows(
        "vigil_r_settle",
        "SELECT count(*) FROM config_audit_log WHERE config_type = 'backup'",
    ) == [(0,)]

    dest_root = root / "dest"
    dest_root.mkdir()
    dest = _empty_layout(dest_root)
    _create_database("vigil_r_settle_dst", ledger=False)
    dest_env = _child_env(dest_root, "vigil_r_settle_dst", **dest)
    dest_env["JWT_SECRET_KEY"] = _STALE_ENV_KEY
    dest_env["RESTIC_CACHE_DIR"] = str(root / "cache")
    restored = _restore(dest_env, repo, passphrase, "--actor", "restore-bot")
    assert restored.returncode == 0, restored.stderr
    assert "approvals expired: 2" in restored.stdout
    assert "integration credentials and user accounts date from the backup" in (
        restored.stdout
    )

    live = "vigil_r_settle_dst"
    audit = _rows(
        live,
        "SELECT config_type, config_key, action, changed_by, "
        "old_value, new_value FROM config_audit_log "
        "WHERE config_type = 'backup'",
    )
    assert len(audit) == 1
    config_type, config_key, action, changed_by, old_value, new_value = audit[0]
    assert (config_type, config_key, action, changed_by, old_value) == (
        "backup",
        "restore",
        "restore",
        "restore-bot",
        None,
    )
    payload = new_value if isinstance(new_value, dict) else json.loads(new_value)
    assert payload["expired_count"] == 2
    assert f"backup date: {payload['created_at']}" in restored.stdout
    assert payload["jwt_rotated"] is True
    reason = (
        f"expired: restored from backup {payload['snapshot_id']} "
        f"taken {payload['created_at']}"
    )
    approvals = _rows(
        live,
        "SELECT action_id, status, rejection_reason, approved_by, "
        "approved_at IS NOT NULL FROM approval_actions ORDER BY action_id",
    )
    by_id = {row[0]: row for row in approvals}
    assert by_id["pend-plain"] == (
        "pend-plain",
        "rejected",
        reason,
        "restore-bot",
        True,
    )
    assert by_id["pend-run"] == ("pend-run", "rejected", reason, "restore-bot", True)
    assert by_id["keep-approved"][1] == "approved"
    assert by_id["keep-approved"][2] is None
    assert by_id["keep-executed"][1] == "executed"
    assert by_id["keep-failed"][1] == "failed"
    assert _rows(
        live, "SELECT status FROM workflow_runs WHERE run_id = 'run-keep'"
    ) == [("paused",)]
    assert _rows(
        "vigil_r_settle",
        "SELECT status FROM approval_actions WHERE action_id = 'pend-plain'",
    ) == [("pending",)]

    stored = json.loads(
        Fernet((dest["state"] / "master.key").read_bytes()).decrypt(
            (dest["state"] / "secrets.enc").read_bytes()
        )
    )
    new_key = stored["JWT_SECRET_KEY"]
    assert stored["token"] == "sekrit"
    assert new_key != _OLD_KEY
    assert new_key != _STALE_ENV_KEY
    assert (dest["state"] / "jwt_secret").read_text(encoding="utf-8") == new_key
    assert (dest["state"] / ".env").read_text(encoding="utf-8") == (
        "KEEP_STATE=yes\n"
        f'JWT_SECRET_KEY="{new_key}"\n'
        f'JWT_SECRET_KEY="{new_key}"\n'
    )
    assert env_path.read_text(encoding="utf-8") == (
        "# sentinel-keep\nOTHER_KEY=leave-me\n"
        f"JWT_SECRET_KEY={new_key}\nJWT_SECRET_KEY={new_key}\n"
    )
    assert {name: _files(path) for name, path in paths.items()} == before_files

    listing = subprocess.check_output(
        ["restic", "-r", str(repo), "snapshots", "--json", "latest"],
        env={**env, "RESTIC_PASSWORD_FILE": str(passphrase)},
        text=True,
    )
    snap = json.loads(listing)[0]
    assert payload["snapshot_id"] == snap["id"]

    mfa = _rows(live, "SELECT mfa_secret FROM users WHERE user_id = 'u-mfa'")[0][0]
    assert mfa != ciphertext
    assert _rows(live, "SELECT mfa_secret FROM users WHERE user_id = 'u-none'") == [
        (None,)
    ]
    old_token = jwt.encode({"sub": "u-mfa"}, _OLD_KEY, algorithm="HS256")
    new_token = jwt.encode({"sub": "u-mfa"}, new_key, algorithm="HS256")
    check_env = dest_env.copy()
    check_env.pop("JWT_SECRET_KEY", None)
    assert _OLD_KEY not in check_env.values()
    checked = subprocess.run(
        [sys.executable, "-c", _CHECK_TOKENS, old_token, new_token, mfa],
        cwd=REPO_ROOT,
        env=check_env,
        text=True,
        capture_output=True,
    )
    assert checked.returncode == 0, checked.stderr
    assert "reject_old True" in checked.stdout
    assert "accept_new u-mfa" in checked.stdout
    assert f"mfa {_TOTP}" in checked.stdout


def test_wrong_old_key_aborts_without_touching_mfa_secrets(
    scratch_databases, tmp_path: Path
):
    root = tmp_path / "wrongkey"
    root.mkdir()
    paths = _layout(root, secret=None, signing_key=False)
    (CORE_REPO_ROOT / ".env").write_text("OTHER_KEY=leave-me\n", encoding="utf-8")
    _create_database("vigil_r_wrongkey", ledger=True)
    # The secret was encrypted under a key other than the one the install holds.
    ciphertext = _mfa_ciphertext(_OLD_KEY, _TOTP)
    _install_app_rows("vigil_r_wrongkey", ciphertext)
    repo = root / "repo"
    passphrase = _passphrase(root / "pass")
    env = _child_env(root, "vigil_r_wrongkey", **paths)
    env["JWT_SECRET_KEY"] = _STALE_ENV_KEY
    created = _create(env, repo, passphrase)
    assert created.returncode == 0, created.stderr

    dest_root = root / "dest"
    dest_root.mkdir()
    dest = _empty_layout(dest_root)
    _create_database("vigil_r_wrongkey_dst", ledger=False)
    dest_env = _child_env(dest_root, "vigil_r_wrongkey_dst", **dest)
    dest_env["JWT_SECRET_KEY"] = _STALE_ENV_KEY
    dest_env["RESTIC_CACHE_DIR"] = str(root / "cache")
    restored = _restore(dest_env, repo, passphrase)

    assert restored.returncode != 0
    assert "u-mfa" in restored.stderr
    for sensitive in (ciphertext, _OLD_KEY, _STALE_ENV_KEY, _TOTP):
        assert sensitive not in restored.stderr
    live = "vigil_r_wrongkey_dst"
    assert _rows(live, "SELECT mfa_secret FROM users WHERE user_id = 'u-mfa'") == [
        (ciphertext,)
    ]
    # The abort comes before any key store is rewritten or approval expired.
    assert not (dest["state"] / "jwt_secret").exists()
    assert (CORE_REPO_ROOT / ".env").read_text(encoding="utf-8") == (
        "OTHER_KEY=leave-me\n"
    )
    assert _rows(
        live, "SELECT status FROM approval_actions WHERE action_id = 'pend-plain'"
    ) == [("pending",)]


def test_key_held_outside_the_install_rotates_into_secrets(
    scratch_databases, tmp_path: Path
):
    root = tmp_path / "outside"
    root.mkdir()
    paths = _layout(root, secret=None, signing_key=False)
    (CORE_REPO_ROOT / ".env").write_text("OTHER_KEY=leave-me\n", encoding="utf-8")
    _create_database("vigil_r_outside", ledger=True)
    ciphertext = _mfa_ciphertext(_STALE_ENV_KEY, _TOTP)
    _install_app_rows("vigil_r_outside", ciphertext)
    repo = root / "repo"
    passphrase = _passphrase(root / "pass")
    # Compose: no file this install holds has the key, only the process env.
    env = _child_env(root, "vigil_r_outside", **paths)
    env["JWT_SECRET_KEY"] = _STALE_ENV_KEY
    created = _create(env, repo, passphrase)
    assert created.returncode == 0, created.stderr

    dest_root = root / "dest"
    dest_root.mkdir()
    dest = _empty_layout(dest_root)
    _create_database("vigil_r_outside_dst", ledger=False)
    dest_env = _child_env(dest_root, "vigil_r_outside_dst", **dest)
    dest_env["JWT_SECRET_KEY"] = _STALE_ENV_KEY
    dest_env["RESTIC_CACHE_DIR"] = str(root / "cache")
    restored = _restore(dest_env, repo, passphrase, "--actor", "restore-bot")

    assert restored.returncode == 0, restored.stderr
    secrets_path = dest["state"] / "secrets.enc"
    assert "approvals expired: 2" in restored.stdout
    assert str(secrets_path) in restored.stdout
    assert "clear or replace JWT_SECRET_KEY in the environment" in restored.stdout
    stored = json.loads(
        Fernet((dest["state"] / "master.key").read_bytes()).decrypt(
            secrets_path.read_bytes()
        )
    )
    new_key = stored["JWT_SECRET_KEY"]
    assert new_key != _STALE_ENV_KEY
    assert new_key not in restored.stdout
    assert _STALE_ENV_KEY not in restored.stdout
    # Only secrets.enc takes the key: no other store held it.
    assert not (dest["state"] / "jwt_secret").exists()
    assert (CORE_REPO_ROOT / ".env").read_text(encoding="utf-8") == (
        "OTHER_KEY=leave-me\n"
    )
    live = "vigil_r_outside_dst"
    statuses = dict(_rows(live, "SELECT action_id, status FROM approval_actions"))
    assert statuses["pend-plain"] == statuses["pend-run"] == "rejected"
    audit = _rows(
        live,
        "SELECT new_value FROM config_audit_log WHERE config_type = 'backup'",
    )
    assert len(audit) == 1
    payload = audit[0][0] if isinstance(audit[0][0], dict) else json.loads(audit[0][0])
    assert payload["jwt_rotated"] is True
    assert payload["expired_count"] == 2
    mfa = _rows(live, "SELECT mfa_secret FROM users WHERE user_id = 'u-mfa'")[0][0]
    assert mfa != ciphertext
    assert (
        Fernet(
            base64.urlsafe_b64encode(hashlib.sha256(new_key.encode()).digest())
        ).decrypt(mfa.encode())
        == _TOTP.encode()
    )


_FAIL_SECOND_DIRECTORY = """
import sys
from core.backup import __main__, restore

real = restore._swap_dir
calls = []

def flaky(swapped, stamp):
    if calls:
        raise OSError("injected failure")
    calls.append(swapped.item.name)
    real(swapped, stamp)

restore._swap_dir = flaky
sys.exit(__main__.main(sys.argv[1:]))
"""


def test_failure_after_first_location_restores_every_original(
    scratch_databases, tmp_path: Path
):
    root = tmp_path / "undo"
    root.mkdir()
    paths = _layout(root, secret=None)
    _create_database("vigil_r_undo", ledger=True)
    repo = root / "repo"
    passphrase = _passphrase(root / "pass")
    created = _create(_child_env(root, "vigil_r_undo", **paths), repo, passphrase)
    assert created.returncode == 0, created.stderr

    dest_root = root / "dest"
    dest_root.mkdir()
    dest = _empty_layout(dest_root)
    (dest["state"] / "old-state.txt").write_text("old-state", encoding="utf-8")
    (dest["workdir"] / "old-work.txt").write_text("old-work", encoding="utf-8")
    _create_database("vigil_r_undo_dst", ledger=False)
    dest_oid = _oid("vigil_r_undo_dst")
    before = {name: _files(path) for name, path in dest.items()}
    before_names = _names()
    proc = _run(
        [
            sys.executable,
            "-c",
            _FAIL_SECOND_DIRECTORY,
            "restore",
            "--repo",
            str(repo),
            "--passphrase-file",
            str(passphrase),
        ],
        _child_env(dest_root, "vigil_r_undo_dst", **dest),
    )
    assert proc.returncode != 0
    assert "injected failure" in proc.stderr
    # The state directory was swapped first and is back, with its own inodes.
    assert {name: _files(path) for name, path in dest.items()} == before
    assert list(dest_root.rglob(".vigil-*")) == []
    assert _oid("vigil_r_undo_dst") == dest_oid
    assert _names() == before_names
