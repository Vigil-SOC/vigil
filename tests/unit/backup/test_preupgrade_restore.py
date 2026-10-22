"""Version rule and disk refusal for a pre-upgrade restore point."""

from __future__ import annotations

import json

import pytest

from core.backup.create import BackupError
from core.backup.preupgrade import _short_disk
from core.backup.restore import _assert_compatible, _record_restored, _version_at_least
from core.config import get_settings
from core.version import __version__

pytestmark = pytest.mark.unit


def test_short_disk_names_the_shortfall() -> None:
    assert _short_disk({"free_bytes": 4, "restore_bytes": 9}) == (
        "not enough disk for a restore point: 4 bytes free, 9 bytes required"
    )
    assert _short_disk({"free_bytes": 9, "restore_bytes": 9}) is None
    assert (
        _short_disk({"restore_bytes": 9})
        == "not enough disk for a restore point: free space is unknown"
    )


def test_pre_upgrade_restores_when_this_image_is_at_least_the_taker() -> None:
    _assert_compatible(
        {"version": "0.0.1", "kind": "pre-upgrade", "taken_by": __version__}
    )
    _assert_compatible({"version": "0.0.0", "kind": "pre-upgrade"})
    with pytest.raises(BackupError, match="newer than this install"):
        _assert_compatible(
            {"version": "0.0.1", "kind": "pre-upgrade", "taken_by": "999.0.0"}
        )
    with pytest.raises(BackupError, match="newer than this install"):
        _assert_compatible({"version": "999.0.0", "kind": "pre-upgrade"})


def test_other_kinds_still_require_the_same_major_minor() -> None:
    core = __version__.split("+", 1)[0].split("-", 1)[0]
    major, minor, *_ = (core.split(".") + ["0", "0"])[:3]
    _assert_compatible({"version": f"{major}.{minor}.99", "kind": "manual"})
    with pytest.raises(BackupError, match="does not match"):
        _assert_compatible(
            {"version": f"{major}.{int(minor) + 1}.0", "kind": "scheduled"}
        )


def test_identical_unparsed_versions_match() -> None:
    assert _version_at_least("dev", "dev") is True
    with pytest.raises(BackupError, match="unreadable version"):
        _version_at_least("0.8.0", "dev")


def test_restored_history_uses_the_upgrade_record_keys(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VIGIL_DISABLE_DOTENV", "1")
    monkeypatch.setenv("VIGIL_UPGRADE_DIR", str(tmp_path))
    get_settings.cache_clear()
    try:
        _record_restored({"version": "0.1.0", "kind": "pre-upgrade"}, "abc", ["job-1"])
        line = json.loads((tmp_path / "history.jsonl").read_text(encoding="utf-8"))
        assert line["outcome"] == "restored"
        assert line["from"] == __version__
        assert line["to"] == "0.1.0"
        assert line["restore_point"] == "abc"
        assert line["reason"] == ""
        assert line["discarded"] == ["job-1"]
        assert set(line) == {
            "time",
            "from",
            "to",
            "outcome",
            "restore_point",
            "reason",
            "discarded",
        }
    finally:
        get_settings.cache_clear()


def test_restored_history_is_skipped_without_the_directory(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("VIGIL_DISABLE_DOTENV", "1")
    monkeypatch.delenv("VIGIL_UPGRADE_DIR", raising=False)
    get_settings.cache_clear()
    try:
        _record_restored({"version": "0.1.0"}, "abc", None)
        assert list(tmp_path.iterdir()) == []
    finally:
        get_settings.cache_clear()
