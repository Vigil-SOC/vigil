"""A restore leaves a trail that verifies, and names the tip it abandoned.

Runs ``python -m core.backup restore`` against Postgres and restic. The
scratch databases in ``test_backup_restore`` are ``create_all`` and have no
revision, so the trail cases build with ``python -m core.upgrade``.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from core.audit import trail
from core.config import REPO_ROOT as CORE_REPO_ROOT
from core.upgrade import command
from test_backup_restore import (
    _child_env,
    _create,
    _create_database,
    _drop_matching,
    _empty_layout,
    _layout,
    _passphrase,
    _restore,
    _rows,
    _url,
)
from test_backup_restore import pytestmark as _restore_marks

pytestmark = _restore_marks


@pytest.fixture
def scratch_databases():
    _drop_matching()
    yield
    _drop_matching()


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


def _upgrade(name: str, work: Path) -> None:
    _create_database(name, ledger=False)
    out = io.StringIO()
    code = command.run(
        _url(name), upgrade_dir=work / "upgrade", restore_point="rp", out=out
    )
    assert code == command.EXIT_OK, out.getvalue()


def _append(database: str, count: int) -> None:
    engine = create_engine(_url(database))
    try:
        with engine.begin() as conn:
            for index in range(count):
                trail.append(conn, {"action": "seed", "i": index})
    finally:
        engine.dispose()


def _chain(database: str) -> list[tuple]:
    return _rows(
        database,
        "SELECT seq, prev_hash, entry_hash, payload::text, operator_text "
        "FROM audit_trail ORDER BY seq",
    )


def _verify(database: str):
    engine = create_engine(_url(database))
    try:
        with engine.connect() as conn:
            checked = trail.verify(conn)
            latest = trail.latest_restore(conn)
            conn.commit()
        return checked, latest
    finally:
        engine.dispose()


def test_restored_chain_verifies_and_names_the_abandoned_tip(
    scratch_databases, tmp_path: Path
):
    root = tmp_path / "chain"
    root.mkdir()
    paths = _layout(root, secret="sekrit")
    live = "vigil_r_chain"
    _upgrade(live, root)
    _append(live, 3)
    snapshot = _chain(live)
    repo = root / "repo"
    passphrase = _passphrase(root / "pass")
    env = _child_env(root, live, **paths)
    created = _create(env, repo, passphrase)
    assert created.returncode == 0, created.stderr

    _append(live, 2)
    tip = _chain(live)[-1]
    abandoned_seq, abandoned_hash = tip[0], tip[2]
    assert abandoned_hash != snapshot[-1][2]

    restored = _restore(env, repo, passphrase, "--actor", "restore-bot")
    assert restored.returncode == 0, restored.stderr

    rows = _chain(live)
    by_seq = {row[0]: row for row in rows}
    for seq, prev_hash, entry_hash, _payload, _operator in snapshot:
        assert by_seq[seq][1] == prev_hash
        assert by_seq[seq][2] == entry_hash
    extra = [row for row in rows if row[0] > snapshot[-1][0]]
    assert len(extra) == 1
    seq, prev_hash, entry_hash, payload_text, operator = extra[0]
    assert prev_hash == snapshot[-1][2]
    assert abandoned_hash not in {row[1] for row in rows}
    assert abandoned_seq not in {row[0] for row in snapshot}
    payload = json.loads(payload_text)
    assert payload[trail.ACTION] == trail.ACTION_RESTORE
    assert payload[trail.ACTOR] == trail.SYSTEM
    assert payload["abandoned"] == {"seq": abandoned_seq, "hash": abandoned_hash}
    assert payload["jwt_rotated"] is True
    assert "outcome" not in payload
    assert operator == "restore-bot"
    assert payload[trail.ACTOR] != operator

    checked, latest = _verify(live)
    assert checked.ok
    assert latest["seq"] == seq
    assert latest["entry_hash"] == entry_hash
    assert latest["operator_text"] == "restore-bot"
    assert latest["payload"]["abandoned"]["hash"] == abandoned_hash
    assert _rows(
        live,
        "SELECT count(*) FROM config_audit_log "
        "WHERE config_type = 'backup' AND action = 'restore'",
    ) == [(0,)]


def test_snapshot_without_the_trail_keeps_the_config_audit_row(
    scratch_databases, tmp_path: Path
):
    from core.storage.models.base import Base

    root = tmp_path / "pre"
    root.mkdir()
    paths = _layout(root, secret="sekrit")
    source = "vigil_r_pre"
    _create_database(source, ledger=True)
    engine = create_engine(_url(source))
    Base.metadata.create_all(engine)
    engine.dispose()
    repo = root / "repo"
    passphrase = _passphrase(root / "pass")
    created = _create(_child_env(root, source, **paths), repo, passphrase)
    assert created.returncode == 0, created.stderr

    dest_root = root / "dest"
    dest_root.mkdir()
    dest = _empty_layout(dest_root)
    live = "vigil_r_pre_dst"
    _create_database(live, ledger=False)
    dest_env = _child_env(dest_root, live, **dest)
    dest_env["RESTIC_CACHE_DIR"] = str(root / "cache")
    restored = _restore(dest_env, repo, passphrase, "--actor", "restore-bot")
    assert restored.returncode == 0, restored.stderr

    assert _rows(live, "SELECT to_regclass('public.audit_trail')") == [(None,)]
    audit = _rows(
        live,
        "SELECT config_type, config_key, action, changed_by, new_value "
        "FROM config_audit_log WHERE config_type = 'backup'",
    )
    assert len(audit) == 1
    config_type, config_key, action, changed_by, new_value = audit[0]
    assert (config_type, config_key, action, changed_by) == (
        "backup",
        "restore",
        "restore",
        "restore-bot",
    )
    payload = new_value if isinstance(new_value, dict) else json.loads(new_value)
    assert payload["jwt_rotated"] is True
    assert "abandoned" not in payload


def test_a_stamped_schema_without_the_trail_table_keeps_the_config_row(
    scratch_databases, tmp_path: Path
):
    # create_all stamps no table for the trail, but a head revision makes
    # trail.trail_ready true. The swap must not query the missing table.
    from core.storage.models.base import Base

    root = tmp_path / "stamp"
    root.mkdir()
    paths = _layout(root, secret="sekrit")
    source = "vigil_r_stamp"
    _create_database(source, ledger=True)
    engine = create_engine(_url(source))
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE public.alembic_version "
                "(version_num varchar(32) NOT NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO alembic_version (version_num) "
                "VALUES ('0002_audit_trail')"
            )
        )
    engine.dispose()
    repo = root / "repo"
    passphrase = _passphrase(root / "pass")
    created = _create(_child_env(root, source, **paths), repo, passphrase)
    assert created.returncode == 0, created.stderr

    dest_root = root / "dest"
    dest_root.mkdir()
    dest = _empty_layout(dest_root)
    live = "vigil_r_stamp_dst"
    _create_database(live, ledger=False)
    dest_env = _child_env(dest_root, live, **dest)
    dest_env["RESTIC_CACHE_DIR"] = str(root / "cache")
    restored = _restore(dest_env, repo, passphrase, "--actor", "restore-bot")
    assert restored.returncode == 0, restored.stderr
    assert _rows(live, "SELECT to_regclass('public.audit_trail')") == [(None,)]
    assert _rows(
        live,
        "SELECT count(*) FROM config_audit_log "
        "WHERE config_type = 'backup' AND action = 'restore'",
    ) == [(1,)]


def test_failed_settle_still_writes_a_failed_trail_entry(
    scratch_databases, tmp_path: Path
):
    root = tmp_path / "fail"
    root.mkdir()
    paths = _layout(root, secret=None, signing_key=False)
    (CORE_REPO_ROOT / ".env").write_text("OTHER_KEY=leave-me\n", encoding="utf-8")
    live = "vigil_r_fail"
    _upgrade(live, root)
    _append(live, 1)
    snapshot = _chain(live)
    repo = root / "repo"
    passphrase = _passphrase(root / "pass")
    env = _child_env(root, live, **paths)
    env.pop("JWT_SECRET_KEY", None)
    created = _create(env, repo, passphrase)
    assert created.returncode == 0, created.stderr

    _append(live, 1)
    tip = _chain(live)[-1]
    abandoned_seq, abandoned_hash = tip[0], tip[2]

    restored = _restore(env, repo, passphrase, "--actor", "restore-bot")
    assert restored.returncode != 0
    assert "previous database:" in restored.stderr
    assert "JWT_SECRET_KEY is not in secrets.enc" in restored.stderr

    rows = _chain(live)
    extra = [row for row in rows if row[0] > snapshot[-1][0]]
    assert len(extra) == 1
    _seq, prev_hash, _entry_hash, payload_text, operator = extra[0]
    assert prev_hash == snapshot[-1][2]
    assert abandoned_hash not in {row[1] for row in rows}
    payload = json.loads(payload_text)
    assert payload[trail.ACTION] == trail.ACTION_RESTORE
    assert payload[trail.ACTOR] == trail.SYSTEM
    assert payload[trail.OUTCOME] == "failed"
    assert "JWT_SECRET_KEY is not in secrets.enc" in payload["reason"]
    assert payload["jwt_rotated"] is False
    assert payload["abandoned"] == {"seq": abandoned_seq, "hash": abandoned_hash}
    assert operator == "restore-bot"
    checked, latest = _verify(live)
    assert checked.ok
    assert latest["payload"][trail.OUTCOME] == "failed"
    assert _rows(
        live,
        "SELECT count(*) FROM config_audit_log "
        "WHERE config_type = 'backup' AND action = 'restore'",
    ) == [(0,)]
