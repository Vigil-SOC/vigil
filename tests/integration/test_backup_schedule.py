"""Scheduled destinations: one due repo, local retention, and the held lock."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg2
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text

from core.backup.create import LOCK_CLASSID, LOCK_OBJID, SKIPPED_MESSAGE
from core.backup.schedule import Destination, _forget

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRATCH_DB = "vigil_test_backup_schedule"


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


needs_db = pytest.mark.skipif(
    not _postgres_available(),
    reason="requires a local PostgreSQL (docker compose up -d postgres)",
)


@pytest.fixture(scope="module")
def scratch_db():
    admin = create_engine(_url("postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {SCRATCH_DB}"))
    owner = create_engine(_url(SCRATCH_DB))
    with owner.connect() as conn:
        conn.execute(text("CREATE TABLE backup_sample (id integer)"))
        conn.execute(text("INSERT INTO backup_sample VALUES (1)"))
        conn.commit()
    yield
    owner.dispose()
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
    admin.dispose()


def _env(root: Path) -> dict[str, str]:
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
            "ORCHESTRATOR_WORKDIR": str(root / "no-work"),
            "VIGIL_INTENT_PATH": str(root / "no.md"),
            "VIGIL_DISABLE_DOTENV": "1",
            "RESTIC_CACHE_DIR": str(root / "cache"),
        }
    )
    env.pop("VIGIL_SKILLS_PATH", None)
    return env


def _write_secret(state: Path, values: dict[str, str]) -> None:
    key = Fernet.generate_key()
    (state / "master.key").write_bytes(key)
    blob = json.dumps(values).encode()
    (state / "secrets.enc").write_bytes(Fernet(key).encrypt(blob))


def _restic_env(env: dict[str, str], passphrase: Path) -> dict[str, str]:
    out = env.copy()
    out["RESTIC_PASSWORD_FILE"] = str(passphrase)
    return out


def _snapshots(env: dict[str, str], repo: Path, passphrase: Path) -> list[dict]:
    raw = subprocess.check_output(
        ["restic", "-r", str(repo), "snapshots", "--json"],
        env=_restic_env(env, passphrase),
        text=True,
    )
    return json.loads(raw)


def _run_due(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from core.backup.schedule import run_due; run_due(bifrost_data=None)",
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=240,
    )


@needs_db
@pytest.mark.database
def test_due_destination_is_tagged_and_the_other_is_left_alone(
    scratch_db, tmp_path: Path
):
    root = tmp_path / "two"
    state = root / "state"
    state.mkdir(parents=True)
    (root / "cache").mkdir()
    due_pass = f"due-pass-{uuid.uuid4().hex}"
    idle_pass = f"idle-pass-{uuid.uuid4().hex}"
    _write_secret(state, {"due_pass": due_pass, "idle_pass": idle_pass})
    due = root / "due"
    idle = root / "idle"
    idle_file = root / "idle.txt"
    idle_file.write_text("idle", encoding="utf-8")
    idle_phrase = root / "idle.pass"
    idle_phrase.write_text(idle_pass, encoding="utf-8")
    env = _env(root)
    restic = _restic_env(env, idle_phrase)
    subprocess.check_call(["restic", "-r", str(idle), "init"], env=restic)
    subprocess.check_call(
        ["restic", "-r", str(idle), "backup", "--tag", "scheduled", str(idle_file)],
        env=restic,
    )
    before = [snap["id"] for snap in _snapshots(env, idle, idle_phrase)]
    (state / "backups.json").write_text(
        json.dumps(
            [
                {
                    "name": "idle",
                    "repo": str(idle),
                    "passphrase_secret": "idle_pass",
                    "interval_hours": 24,
                    "keep_last": 14,
                    "default": False,
                },
                {
                    "name": "due",
                    "repo": str(due),
                    "passphrase_secret": "due_pass",
                    "interval_hours": 1,
                    "keep_last": 14,
                    "default": True,
                },
            ]
        ),
        encoding="utf-8",
    )

    proc = _run_due(env)

    assert proc.returncode == 0, proc.stderr
    assert [snap["id"] for snap in _snapshots(env, idle, idle_phrase)] == before
    due_phrase = root / "due.pass"
    due_phrase.write_text(due_pass, encoding="utf-8")
    snaps = _snapshots(env, due, due_phrase)
    assert len(snaps) == 1
    assert snaps[0].get("tags") == ["scheduled"]
    listing = subprocess.check_output(
        ["restic", "-r", str(due), "ls", snaps[0]["id"]],
        env=_restic_env(env, due_phrase),
        text=True,
    )
    manifest_path = next(
        line for line in listing.splitlines() if line.endswith("/manifest.json")
    )
    manifest = json.loads(
        subprocess.check_output(
            ["restic", "-r", str(due), "dump", snaps[0]["id"], manifest_path],
            env=_restic_env(env, due_phrase),
        )
    )
    assert manifest["kind"] == "scheduled"
    config = (state / "backups.json").read_text(encoding="utf-8")
    assert due_pass not in config
    assert idle_pass not in config
    for path in due.rglob("*"):
        if path.is_file() and due_pass.encode() in path.read_bytes():
            raise AssertionError(f"passphrase appeared in {path}")
    status = json.loads((state / "backup_status.json").read_text(encoding="utf-8"))
    assert status["destination"] == "due"
    assert status["snapshot_id"] == snaps[0]["id"]


def test_local_retention_drops_scheduled_and_keeps_pre_upgrade(tmp_path: Path):
    root = tmp_path / "retain"
    root.mkdir()
    repo = root / "repo"
    phrase = root / "pass"
    phrase.write_text("retain-pass", encoding="utf-8")
    env = os.environ.copy()
    env["RESTIC_PASSWORD_FILE"] = str(phrase)
    env["RESTIC_CACHE_DIR"] = str(root / "cache")
    (root / "cache").mkdir(parents=True)
    subprocess.check_call(["restic", "-r", str(repo), "init"], env=env)
    seeds = [
        ("scheduled", "2020-01-01 00:00:00", "old"),
        ("scheduled", "2020-06-01 00:00:00", "newer"),
        ("manual", "2021-01-01 00:00:00", "by-hand"),
        ("pre-upgrade", "2022-01-01 00:00:00", "upgrade"),
        ("safety", "2022-06-01 00:00:00", "safety"),
    ]
    for tag, when, name in seeds:
        path = root / name
        path.write_text(name, encoding="utf-8")
        subprocess.check_call(
            [
                "restic",
                "-r",
                str(repo),
                "backup",
                "--tag",
                tag,
                "--time",
                when,
                str(path),
            ],
            env=env,
        )

    _forget(
        Destination(
            name="disk",
            repo=str(repo),
            passphrase_secret="unused",
            interval_hours=1,
            keep_last=2,
            default=True,
            must_be_mount=False,
        ),
        phrase,
    )

    kept = _snapshots(env, repo, phrase)
    assert sorted(tuple(snap.get("tags") or []) for snap in kept) == [
        ("manual",),
        ("pre-upgrade",),
        ("safety",),
        ("scheduled",),
    ]
    scheduled = [snap for snap in kept if snap.get("tags") == ["scheduled"]]
    assert len(scheduled) == 1
    assert scheduled[0]["paths"][0].endswith("/newer")


@needs_db
@pytest.mark.database
def test_locked_run_logs_the_skip_and_does_not_dump(scratch_db, tmp_path: Path):
    root = tmp_path / "lock"
    state = root / "state"
    state.mkdir(parents=True)
    (root / "cache").mkdir()
    _write_secret(state, {"due_pass": "lock-pass"})
    repo = root / "repo"
    (state / "backups.json").write_text(
        json.dumps(
            [
                {
                    "name": "due",
                    "repo": str(repo),
                    "passphrase_secret": "due_pass",
                    "interval_hours": 1,
                    "keep_last": 2,
                    "default": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    part = _parts()
    held = psycopg2.connect(
        host=part["host"],
        port=part["port"],
        user=part["user"],
        password=part["password"],
        dbname=SCRATCH_DB,
    )
    held.autocommit = True
    cursor = held.cursor()
    cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", (LOCK_CLASSID, LOCK_OBJID))
    assert cursor.fetchone()[0] is True
    try:
        proc = _run_due(_env(root))
    finally:
        held.close()

    assert proc.returncode == 0, proc.stderr
    assert SKIPPED_MESSAGE in proc.stderr
    assert not (repo / "config").exists()
    assert not (state / "backup_status.json").exists()
