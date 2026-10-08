"""``python -m core.backup pre-upgrade`` and the schema stamp ``init_database`` writes."""

from __future__ import annotations

import json
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
    _restore,
    _run,
    _url,
)
from test_backup_restore import pytestmark as _restore_marks

from core.storage.models.config import SystemConfig

# Same database mark and skip-without-postgres as the restore tests.
pytestmark = _restore_marks

DATABASE = "vigil_r_preupgrade"
PASSPHRASE = "pre-upgrade-pass"
SKIP = "VIGIL_SKIP_PREUPGRADE_BACKUP"

# Versions are derived from VERSION so a release bump never breaks these tests.
# A restore only runs on the release that made the backup, and the restore here
# runs on the checked-out code, so the stamped database must be on this release
# (but not this exact version, so a re-stamp is observable) and the upgrade
# target on the next one.
CURRENT = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
_CORE = CURRENT.split("+", 1)[0].split("-", 1)[0]
_MAJOR, _MINOR, _PATCH = (_CORE.split(".") + ["0", "0"])[:3]
STAMPED = f"{_MAJOR}.{_MINOR}.{0 if _PATCH != '0' else 1}"
SAME_RELEASE = f"{_MAJOR}.{_MINOR}.99"
NEXT = f"{_MAJOR}.{int(_MINOR) + 1}.0"


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
    env = _child_env(root, DATABASE, **paths)
    yield env, repo, passphrase, state
    admin = create_engine(_url("postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{DATABASE}" WITH (FORCE)'))
    admin.dispose()


def _pre_upgrade(env: dict[str, str], target: str):
    return _run(
        [
            sys.executable,
            "-m",
            "core.backup",
            "pre-upgrade",
            "--target-version",
            target,
        ],
        env,
    )


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


def test_snapshot_records_the_stamp_and_restores_on_the_old_release(instance):
    env, repo, passphrase, _ = instance

    proc = _pre_upgrade(env, NEXT)

    assert proc.returncode == 0, proc.stderr
    manifest = _manifest(env, repo, passphrase)
    assert manifest["kind"] == "pre-upgrade"
    assert manifest["version"] == STAMPED
    # The code under test is on the stamped release, as the rollback target would be.
    tested = _restore(env, repo, passphrase, "--test")
    assert tested.returncode == 0, tested.stderr


@pytest.mark.parametrize(
    "case", ["same-release", "no-stamp", "no-destination", "dev-build"]
)
def test_nothing_due_takes_no_snapshot(instance, case):
    env, repo, _, state = instance
    target = {"same-release": SAME_RELEASE, "dev-build": "dev"}.get(case, NEXT)
    if case == "no-stamp":
        _stamp(DATABASE, None)
    if case == "no-destination":
        (state / "backups.json").unlink()

    proc = _pre_upgrade(env, target)

    assert proc.returncode == 0, proc.stderr
    assert not repo.exists()


def test_unusable_destinations_file_fails_instead_of_skipping(instance):
    env, _, _, state = instance
    (state / "backups.json").write_text("{not json", encoding="utf-8")

    proc = _pre_upgrade(env, NEXT)

    assert proc.returncode != 0
    assert "no usable destination" in proc.stderr
    assert SKIP in proc.stderr


def test_failed_snapshot_names_the_override_and_the_override_skips_it(instance):
    env, repo, _, _ = instance
    other = repo.parent / "other-pass"
    other.write_text("a different passphrase", encoding="utf-8")
    subprocess.check_call(
        ["restic", "-r", str(repo), "init"],
        env={**env, "RESTIC_PASSWORD_FILE": str(other)},
    )

    failed = _pre_upgrade(env, NEXT)
    skipped = _pre_upgrade({**env, SKIP: "1"}, NEXT)

    assert failed.returncode != 0
    assert SKIP in failed.stderr
    assert skipped.returncode == 0, skipped.stderr
    assert SKIP in skipped.stderr


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
