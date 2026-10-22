"""``python -m core.backup pre-upgrade`` and the schema stamp ``init_database`` writes."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text

# Sibling module: tests/integration is not a package.
from test_backup_restore import (
    REPO_ROOT,
    _child_env,
    _create_database,
    _layout,
    _run,
    _url,
)
from test_backup_restore import pytestmark as _restore_marks

from core.storage.models.config import SystemConfig
from core.upgrade.command import script_directory
from core.version import __version__

# Same database mark and skip-without-postgres as the restore tests.
pytestmark = _restore_marks

DATABASE = "vigil_r_preupgrade"
EMPTY = "vigil_r_preupgrade_empty"
PASSPHRASE = "pre-upgrade-pass"
# Stamped as the database's revision: nothing pending.
HEAD = script_directory().get_current_head()

# The manifest records the database stamp, which is this release but not this
# exact version, so a re-stamp would show. The image under test is CURRENT.
CURRENT = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
_CORE = CURRENT.split("+", 1)[0].split("-", 1)[0]
_MAJOR, _MINOR, _PATCH = (_CORE.split(".") + ["0", "0"])[:3]
STAMPED = f"{_MAJOR}.{_MINOR}.{0 if _PATCH != '0' else 1}"
PREVIOUS = "0.0.1"


def _stamp(database: str, version: str | None) -> None:
    engine = create_engine(_url(database))
    with engine.begin() as conn:
        SystemConfig.__table__.create(conn, checkfirst=True)
        conn.execute(text("DELETE FROM system_config WHERE key = 'schema_version'"))
        if version is not None:
            conn.execute(
                text(
                    "INSERT INTO system_config (key, value) "
                    "VALUES ('schema_version', CAST(:v AS jsonb))"
                ),
                {"v": json.dumps({"version": version})},
            )
    engine.dispose()


def _stamped(database: str) -> str | None:
    engine = create_engine(_url(database))
    with engine.connect() as conn:
        value = conn.execute(
            text("SELECT value FROM system_config WHERE key = 'schema_version'")
        ).scalar()
    engine.dispose()
    return value["version"] if value else None


def _at_head(database: str) -> None:
    engine = create_engine(_url(database))
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version "
                "(version_num VARCHAR(32) NOT NULL)"
            )
        )
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
            {"v": HEAD},
        )
    engine.dispose()


def _history(upgrade: Path, to: str) -> None:
    upgrade.mkdir(parents=True, exist_ok=True)
    entry = {
        "time": "2020-01-01T00:00:00+00:00",
        "from": None,
        "to": to,
        "outcome": "applied",
        "restore_point": "old",
        "reason": "",
        "discarded": [],
    }
    (upgrade / "history.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")


def _drop(name: str) -> None:
    admin = create_engine(_url("postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def instance(tmp_path: Path):
    """A database stamped STAMPED with a default destination in ``backups.json``."""
    _create_database(DATABASE, ledger=True)
    _stamp(DATABASE, STAMPED)
    root = tmp_path
    paths = _layout(root, secret=None)
    state = paths["state"]
    key = Fernet.generate_key()
    (state / "master.key").write_bytes(key)
    secrets = json.dumps({"dest_pass": PASSPHRASE}).encode()
    (state / "secrets.enc").write_bytes(Fernet(key).encrypt(secrets))
    repo = root / "repo"
    (state / "backups.json").write_text(
        json.dumps(
            [
                {
                    "name": "default",
                    "repo": str(repo),
                    "passphrase_secret": "dest_pass",
                    "default": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    passphrase = root / "pass"
    passphrase.write_text(PASSPHRASE, encoding="utf-8")
    upgrade = root / "upgrade"
    upgrade.mkdir()
    env = _child_env(root, DATABASE, **paths)
    env["VIGIL_UPGRADE_DIR"] = str(upgrade)
    yield env, repo, passphrase, state, upgrade
    _drop(DATABASE)


def _pre_upgrade(env: dict[str, str]):
    return _run([sys.executable, "-m", "core.backup", "pre-upgrade"], env)


def _manifest(env: dict[str, str], repo: Path, passphrase: Path) -> dict:
    env = {**env, "RESTIC_PASSWORD_FILE": str(passphrase)}
    snaps = json.loads(
        subprocess.check_output(
            ["restic", "-r", str(repo), "snapshots", "--json"], env=env
        )
    )
    assert len(snaps) == 1
    assert snaps[0]["tags"] == ["pre-upgrade"]
    listing = subprocess.check_output(
        ["restic", "-r", str(repo), "ls", snaps[0]["id"]], env=env, text=True
    )
    path = next(x for x in listing.splitlines() if x.endswith("/manifest.json"))
    dump = subprocess.check_output(
        ["restic", "-r", str(repo), "dump", snaps[0]["id"], path], env=env
    )
    return json.loads(dump)


def _due(upgrade: Path) -> None:
    _at_head(DATABASE)
    _history(upgrade, PREVIOUS)


_SPY = """
import json
import os
import sys
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url

from core.backup.restore import _check_ledger, restore_snapshot
from core.storage.connection import DatabaseManager

seen = Path(os.environ["SPY_OUT"])
real = _check_ledger


def spy(cfg, database):
    engine = create_engine(make_url(cfg.get_database_url()).set(database=database))
    with engine.connect() as conn:
        value = conn.execute(
            text("SELECT value FROM system_config WHERE key = 'schema_version'")
        ).scalar()
        tables = [
            row[0]
            for row in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        ]
    engine.dispose()
    seen.write_text(json.dumps({"version": value, "tables": tables}), encoding="utf-8")
    return real(cfg, database)


def boom(*_args, **_kwargs):
    raise AssertionError("schema write")


with (
    mock.patch("core.backup.restore._check_ledger", spy),
    mock.patch("core.storage.connection.init_database", boom),
    mock.patch.object(DatabaseManager, "create_tables", boom),
):
    restore_snapshot(
        repo=sys.argv[1],
        passphrase_file=sys.argv[2],
        snapshot="latest",
        test=True,
    )
"""


def test_snapshot_records_the_stamp_and_restores_without_create_all(instance):
    env, repo, passphrase, _, upgrade = instance
    _due(upgrade)

    proc = _pre_upgrade(env)

    assert proc.returncode == 0, proc.stderr
    manifest = _manifest(env, repo, passphrase)
    assert manifest["kind"] == "pre-upgrade"
    assert manifest["version"] == STAMPED
    assert manifest["taken_by"] == CURRENT == __version__
    spy_out = upgrade / "spy.json"
    restored = _run(
        [sys.executable, "-c", _SPY, str(repo), str(passphrase)],
        {**env, "SPY_OUT": str(spy_out)},
    )
    assert restored.returncode == 0, restored.stderr
    seen = json.loads(spy_out.read_text(encoding="utf-8"))
    assert seen["version"]["version"] == STAMPED
    assert "users" not in seen["tables"]
    assert _stamped(DATABASE) == STAMPED


def test_restart_takes_no_point(instance):
    env, repo, _, _, _ = instance
    _at_head(DATABASE)

    proc = _pre_upgrade(env)

    assert proc.returncode == 0, proc.stderr
    assert not repo.exists()


def test_refuse_without_alembic_takes_no_point(instance):
    env, repo, _, _, _ = instance

    proc = _pre_upgrade(env)

    assert proc.returncode == 0, proc.stderr
    assert not repo.exists()


def test_no_destination_creates_default_repo(instance):
    env, configured, _, state, upgrade = instance
    (state / "backups.json").unlink()
    _due(upgrade)

    proc = _pre_upgrade(env)

    assert proc.returncode == 0, proc.stderr
    assert not configured.exists()
    repo = upgrade / "repo"
    passphrase = upgrade / "passphrase"
    assert (repo / "config").is_file()
    assert passphrase.is_file()
    assert not passphrase.is_relative_to(repo)
    assert stat.S_IMODE(passphrase.stat().st_mode) == 0o600
    secret = passphrase.read_text(encoding="utf-8")
    manifest = _manifest(env, repo, passphrase)
    assert manifest["version"] == STAMPED
    assert manifest["taken_by"] == CURRENT
    assert secret not in json.dumps(manifest)
    listing = subprocess.check_output(
        ["restic", "-r", str(repo), "ls", "latest"],
        env={**env, "RESTIC_PASSWORD_FILE": str(passphrase)},
        text=True,
    )
    assert "passphrase" not in listing
    for path in repo.rglob("*"):
        if path.is_file() and secret.encode() in path.read_bytes():
            raise AssertionError(f"passphrase appeared in {path}")


def test_pending_revision_takes_a_point(tmp_path: Path):
    _create_database(EMPTY, ledger=False)
    try:
        root = tmp_path / "empty"
        paths = _layout(root, secret=None)
        upgrade = root / "upgrade"
        upgrade.mkdir()
        env = _child_env(root, EMPTY, **paths)
        env["VIGIL_UPGRADE_DIR"] = str(upgrade)
        proc = _pre_upgrade(env)
        assert proc.returncode == 0, proc.stderr
        manifest = _manifest(env, upgrade / "repo", upgrade / "passphrase")
        assert manifest["kind"] == "pre-upgrade"
        assert manifest["version"] == "0.0.0"
        assert manifest["taken_by"] == CURRENT
    finally:
        _drop(EMPTY)


def test_unusable_destinations_file_fails_instead_of_skipping(instance):
    env, _, _, state, _ = instance
    (state / "backups.json").write_text("{not json", encoding="utf-8")

    proc = _pre_upgrade(env)

    assert proc.returncode != 0
    assert "no usable destination" in proc.stderr
    assert "VIGIL_SKIP_PREUPGRADE_BACKUP" not in proc.stderr


def test_failed_snapshot_refuses_and_ack_takes_none(instance):
    env, repo, _, _, upgrade = instance
    _due(upgrade)
    other = repo.parent / "other-pass"
    other.write_text("a different passphrase", encoding="utf-8")
    subprocess.check_call(
        ["restic", "-r", str(repo), "init"],
        env={**env, "RESTIC_PASSWORD_FILE": str(other)},
    )

    failed = _pre_upgrade(env)
    acked = _pre_upgrade(
        {
            **env,
            "VIGIL_UPGRADE_ACK_VERSION": CURRENT,
            "VIGIL_UPGRADE_ACK_REASON": "operator backup",
        }
    )

    assert failed.returncode != 0
    assert "VIGIL_SKIP_PREUPGRADE_BACKUP" not in failed.stderr
    assert acked.returncode == 0, acked.stderr
    snaps = json.loads(
        subprocess.check_output(
            ["restic", "-r", str(repo), "snapshots", "--json"],
            env={**env, "RESTIC_PASSWORD_FILE": str(other)},
        )
    )
    assert snaps == []


_INIT = """
from unittest import mock
from core.storage.connection import DatabaseManager, init_database
if {fail}:
    with mock.patch.object(DatabaseManager, "create_tables", side_effect=RuntimeError("boom")):
        init_database()
else:
    init_database()
"""


def test_init_database_stamps_only_after_create_all(instance):
    env, *_ = instance

    failed = _run([sys.executable, "-c", _INIT.format(fail=True)], env)
    assert failed.returncode != 0
    assert _stamped(DATABASE) == STAMPED

    ok = _run([sys.executable, "-c", _INIT.format(fail=False)], env)
    assert ok.returncode == 0, ok.stderr
    assert _stamped(DATABASE) == CURRENT
