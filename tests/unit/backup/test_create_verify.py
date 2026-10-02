"""A failed verification removes the snapshot it just wrote."""

from __future__ import annotations

import pytest

from core.backup.create import BackupError, create_snapshot

pytestmark = pytest.mark.unit


class _Conn:
    def close(self) -> None:
        return None


def _install(monkeypatch, verify) -> tuple[list, list]:
    backed: list = []
    forgotten: list = []
    monkeypatch.setattr("core.backup.create._require_tool", lambda name: None)
    monkeypatch.setattr("core.backup.create.backup_database_config", lambda: object())
    monkeypatch.setattr("core.backup.create._priority_prefix", lambda: [])
    monkeypatch.setattr("core.backup.create._connect", lambda cfg, autocommit: _Conn())
    monkeypatch.setattr("core.backup.create._try_lock", lambda conn: True)
    monkeypatch.setattr("core.backup.create._require_pg_dump_version", lambda *a: None)
    monkeypatch.setattr("core.backup.create._ensure_repo", lambda *a: None)
    monkeypatch.setattr(
        "core.backup.create._export_and_count", lambda conn: ("pg-snap", {})
    )
    monkeypatch.setattr("core.backup.create._pg_dump", lambda *a: None)
    monkeypatch.setattr("core.backup.create._locations", lambda staging, bifrost: [])

    def backup(*args):
        backed.append(args[-1])
        return "deadbeef" * 8

    monkeypatch.setattr("core.backup.create._restic_backup", backup)
    monkeypatch.setattr("core.backup.create._verify", verify)
    monkeypatch.setattr(
        "core.backup.create._forget_snapshot",
        lambda *args: forgotten.append(args[2]) or None,
    )
    return backed, forgotten


def test_failed_verify_removes_the_snapshot_and_does_not_tag(tmp_path, monkeypatch):
    phrase = tmp_path / "pw"
    phrase.write_text("secret", encoding="utf-8")

    def verify(*args):
        raise BackupError("verification failed: manifest")

    backed, forgotten = _install(monkeypatch, verify)

    with pytest.raises(BackupError, match="verification failed: manifest"):
        create_snapshot(
            repo=str(tmp_path / "repo"),
            passphrase_file=str(phrase),
            bifrost_data=None,
            kind="scheduled",
            tags=("scheduled",),
        )

    assert backed == [("scheduled",)]
    assert forgotten == ["deadbeef" * 8]


def test_successful_verify_keeps_the_snapshot(tmp_path, monkeypatch):
    phrase = tmp_path / "pw"
    phrase.write_text("secret", encoding="utf-8")
    backed, forgotten = _install(monkeypatch, lambda *args: None)

    snapshot = create_snapshot(
        repo=str(tmp_path / "repo"),
        passphrase_file=str(phrase),
        bifrost_data=None,
        tags=("scheduled",),
    )

    assert snapshot == "deadbeef" * 8
    assert backed == [("scheduled",)]
    assert forgotten == []
