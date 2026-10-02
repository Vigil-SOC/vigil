"""The schedule loop: due is per destination, and cloud repos are not pruned."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.backup.create import SKIPPED_MESSAGE, BackupError
from core.backup.schedule import (
    _latest_snapshot_at,
    _load_destinations,
    _parse_time,
    run_due,
)

pytestmark = pytest.mark.unit

PASSPHRASE = "schedule-passphrase-not-for-json"
WHEN = datetime(2026, 10, 2, 4, 8, tzinfo=timezone.utc)


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_DIR", str(tmp_path))
    return tmp_path


def _destinations(state: Path, entries: list[dict]) -> None:
    (state / "backups.json").write_text(json.dumps(entries), encoding="utf-8")


def _entry(name: str, repo: str, **extra) -> dict:
    item = {
        "name": name,
        "repo": repo,
        "passphrase_secret": f"{name}_pass",
        "interval_hours": 24,
        "keep_last": 14,
        "default": name == "due",
    }
    item.update(extra)
    return item


def _patch_pass(monkeypatch, value: str | None = PASSPHRASE) -> None:
    monkeypatch.setattr(
        "core.backup.schedule.get_secret",
        lambda key: value,
    )


def test_missing_or_empty_file_stays_in_the_loop(state, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "core.backup.schedule.create_snapshot",
        lambda **kwargs: calls.append(kwargs),
    )

    run_due(bifrost_data=None)
    _destinations(state, [])
    run_due(bifrost_data=None)

    assert calls == []


def test_only_the_due_destination_runs(state, monkeypatch):
    due = "/backup/due"
    idle = "/backup/idle"
    _destinations(state, [_entry("idle", idle), _entry("due", due)])
    _patch_pass(monkeypatch)
    seen: list[str] = []

    def latest(repo: str, passphrase: Path, extra=None) -> datetime | None:
        if repo == idle:
            return datetime.now(timezone.utc)
        return None

    def create(**kwargs):
        seen.append(kwargs["repo"])
        assert kwargs["kind"] == "scheduled"
        assert kwargs["tags"] == ("scheduled",)
        assert kwargs["bifrost_data"] == "/var/lib/vigil/bifrost"
        path = Path(kwargs["passphrase_file"])
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.read_text(encoding="utf-8") == PASSPHRASE
        return "scheduled-snap"

    monkeypatch.setattr("core.backup.schedule._latest_snapshot_at", latest)
    monkeypatch.setattr("core.backup.schedule.create_snapshot", create)
    monkeypatch.setattr(
        "core.backup.schedule._forget",
        lambda dest, passphrase, extra=None: None,
    )

    run_due(bifrost_data="/var/lib/vigil/bifrost")

    assert seen == [due]
    assert PASSPHRASE not in (state / "backups.json").read_text(encoding="utf-8")
    status = json.loads((state / "backup_status.json").read_text(encoding="utf-8"))
    assert status["destination"] == "due"
    assert status["snapshot_id"] == "scheduled-snap"
    assert status["last_success_at"]


def test_status_file_does_not_mark_another_destination_not_due(state, monkeypatch):
    (state / "backup_status.json").write_text(
        json.dumps(
            {
                "destination": "idle",
                "last_success_at": datetime.now(timezone.utc).isoformat(),
                "snapshot_id": "previous",
            }
        ),
        encoding="utf-8",
    )
    _destinations(
        state,
        [_entry("idle", "/backup/idle"), _entry("due", "/backup/due")],
    )
    _patch_pass(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(
        "core.backup.schedule._latest_snapshot_at",
        lambda repo, passphrase, extra=None: None,
    )
    monkeypatch.setattr(
        "core.backup.schedule.create_snapshot",
        lambda **kwargs: seen.append(kwargs["repo"]) or "new",
    )
    monkeypatch.setattr(
        "core.backup.schedule._forget",
        lambda dest, passphrase, extra=None: None,
    )

    run_due(bifrost_data=None)

    assert seen == ["/backup/idle", "/backup/due"]


def test_s3_repo_does_not_forget_or_prune(state, monkeypatch):
    _destinations(state, [_entry("cloud", "s3:bucket/prefix", keep_last=3)])
    _patch_pass(monkeypatch)
    commands: list[list[str]] = []

    def capture(args, **kwargs):
        commands.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    created: list[str] = []
    monkeypatch.setattr(
        "core.backup.schedule._latest_snapshot_at",
        lambda repo, passphrase, extra=None: None,
    )
    monkeypatch.setattr(
        "core.backup.schedule.create_snapshot",
        lambda **kwargs: created.append(kwargs["repo"]) or "cloud-snap",
    )
    monkeypatch.setattr("core.backup.schedule._run", capture)

    run_due(bifrost_data=None)

    assert created == ["s3:bucket/prefix"]
    assert commands == []
    flat = " ".join(" ".join(cmd) for cmd in commands)
    assert "forget" not in flat
    assert "prune" not in flat


def test_local_forget_uses_or_tags(state, monkeypatch):
    _destinations(state, [_entry("disk", "/backup/disk", keep_last=2)])
    _patch_pass(monkeypatch)
    commands: list[list[str]] = []

    def capture(args, **kwargs):
        commands.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(
        "core.backup.schedule._latest_snapshot_at",
        lambda repo, passphrase, extra=None: None,
    )
    monkeypatch.setattr(
        "core.backup.schedule.create_snapshot",
        lambda **kwargs: "local-snap",
    )
    monkeypatch.setattr("core.backup.schedule._run", capture)

    run_due(bifrost_data=None)

    forget = next(cmd for cmd in commands if "forget" in cmd)
    tags = [forget[i + 1] for i, arg in enumerate(forget) if arg == "--tag"]
    assert tags == ["manual", "scheduled"]
    assert "manual,scheduled" not in forget
    assert "--prune" in forget
    assert forget[forget.index("--keep-last") + 1] == "2"
    assert forget[forget.index("--group-by") + 1] == ""


def test_must_be_mount_fails_before_create(state, monkeypatch, caplog):
    repo = str(state / "not-a-mount")
    _destinations(state, [_entry("usb", repo, must_be_mount=True)])
    _patch_pass(monkeypatch)
    previous = {
        "destination": "usb",
        "last_success_at": WHEN.isoformat(),
        "snapshot_id": "kept",
    }
    (state / "backup_status.json").write_text(json.dumps(previous), encoding="utf-8")

    def create(**kwargs):
        raise AssertionError("create ran")

    monkeypatch.setattr("core.backup.schedule.create_snapshot", create)

    with caplog.at_level(logging.ERROR):
        run_due(bifrost_data=None)

    assert "not a mount" in caplog.text
    assert (
        json.loads((state / "backup_status.json").read_text(encoding="utf-8"))
        == previous
    )


def test_failure_leaves_the_status_file(state, monkeypatch, caplog):
    _destinations(state, [_entry("due", "/backup/due")])
    _patch_pass(monkeypatch)
    previous = {
        "destination": "older",
        "last_success_at": WHEN.isoformat(),
        "snapshot_id": "kept",
    }
    (state / "backup_status.json").write_text(json.dumps(previous), encoding="utf-8")
    monkeypatch.setattr(
        "core.backup.schedule._latest_snapshot_at",
        lambda repo, passphrase, extra=None: None,
    )

    def create(**kwargs):
        raise BackupError("disk full")

    monkeypatch.setattr("core.backup.schedule.create_snapshot", create)

    with caplog.at_level(logging.ERROR):
        run_due(bifrost_data=None)

    assert "disk full" in caplog.text
    assert PASSPHRASE not in caplog.text
    assert (
        json.loads((state / "backup_status.json").read_text(encoding="utf-8"))
        == previous
    )


def test_held_lock_logs_the_skip(state, monkeypatch, caplog):
    from core.backup.create import BackupSkipped

    _destinations(state, [_entry("due", "/backup/due")])
    _patch_pass(monkeypatch)
    monkeypatch.setattr(
        "core.backup.schedule._latest_snapshot_at",
        lambda repo, passphrase, extra=None: None,
    )

    def create(**kwargs):
        raise BackupSkipped()

    monkeypatch.setattr("core.backup.schedule.create_snapshot", create)

    with caplog.at_level(logging.WARNING):
        run_due(bifrost_data=None)

    assert SKIPPED_MESSAGE in caplog.text
    assert not (state / "backup_status.json").exists()


def test_untagged_snapshot_is_not_a_success(state, monkeypatch):
    repo = state / "repo"
    repo.mkdir()
    (repo / "config").write_text("restic-config", encoding="utf-8")
    phrase = state / "pw"
    phrase.write_text("x", encoding="utf-8")
    payload = json.dumps(
        [
            {"time": "2026-10-02T04:00:00.123456789Z", "tags": ["scheduled"]},
            {"time": "2026-10-02T06:00:00Z"},
        ]
    )

    def capture(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")

    monkeypatch.setattr("core.backup.schedule._run", capture)
    latest = _latest_snapshot_at(str(repo), phrase)
    assert latest == datetime(2026, 10, 2, 4, 0, 0, 123456, tzinfo=timezone.utc)


def test_parse_restic_time_keeps_microseconds():
    parsed = _parse_time("2026-10-02T04:08:01.123456789Z")
    assert parsed == datetime(2026, 10, 2, 4, 8, 1, 123456, tzinfo=timezone.utc)
    assert _parse_time("nope") is None


@pytest.mark.parametrize(
    "env",
    [
        ["AWS_ACCESS_KEY_ID"],
        {"AWS_ACCESS_KEY_ID": 5},
        {"AWS_ACCESS_KEY_ID": ""},
        {"": "key"},
        {"RESTIC_PASSWORD_FILE": "other"},
        {"restic_password_command": "other"},
    ],
)
def test_bad_env_skips_the_destination(state, caplog, env):
    _destinations(state, [_entry("cloud", "s3:bucket/p", env=env)])

    with caplog.at_level(logging.ERROR):
        assert _load_destinations() == []

    assert "destination cloud" in caplog.text


def test_env_is_optional_and_parsed_to_pairs(state):
    _destinations(
        state,
        [
            _entry("plain", "/r"),
            _entry("cloud", "s3:b/p", env={"AWS_ACCESS_KEY_ID": "ak"}),
        ],
    )

    plain, cloud = _load_destinations()

    assert plain.env == ()
    assert cloud.env == (("AWS_ACCESS_KEY_ID", "ak"),)


def test_env_secrets_reach_restic_only_and_not_os_environ(state, monkeypatch):
    _destinations(
        state,
        [_entry("cloud", "s3:b/p", env={"AWS_ACCESS_KEY_ID": "ak_key"})],
    )
    secrets = {"cloud_pass": PASSPHRASE, "ak_key": "AKIA-VALUE"}
    monkeypatch.setattr("core.backup.schedule.get_secret", secrets.get)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    seen: list[dict] = []

    def capture(args, **kwargs):
        seen.append(kwargs["env"])
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("core.backup.schedule._run", capture)
    monkeypatch.setattr(
        "core.backup.schedule.create_snapshot",
        lambda **kwargs: seen.append({"extra": kwargs["extra_env"]}) or "snap",
    )

    run_due(bifrost_data=None)

    assert seen[0]["AWS_ACCESS_KEY_ID"] == "AKIA-VALUE"
    assert seen[0]["RESTIC_PASSWORD_FILE"]
    assert seen[1] == {"extra": {"AWS_ACCESS_KEY_ID": "AKIA-VALUE"}}
    assert "AWS_ACCESS_KEY_ID" not in os.environ
    assert "AKIA-VALUE" not in (state / "backup_status.json").read_text(
        encoding="utf-8"
    )


def test_missing_env_secret_names_the_key_and_others_still_run(
    state, monkeypatch, caplog
):
    _destinations(
        state,
        [
            _entry("cloud", "s3:b/p", env={"AWS_ACCESS_KEY_ID": "ak_key"}),
            _entry("disk", "/backup/disk"),
        ],
    )
    secrets = {"cloud_pass": PASSPHRASE, "disk_pass": PASSPHRASE}
    monkeypatch.setattr("core.backup.schedule.get_secret", secrets.get)
    monkeypatch.setattr(
        "core.backup.schedule._latest_snapshot_at",
        lambda repo, passphrase, extra=None: None,
    )
    created: list[str] = []
    monkeypatch.setattr(
        "core.backup.schedule.create_snapshot",
        lambda **kwargs: created.append(kwargs["repo"]) or "snap",
    )
    monkeypatch.setattr(
        "core.backup.schedule._forget",
        lambda dest, passphrase, extra=None: None,
    )

    with caplog.at_level(logging.ERROR):
        run_due(bifrost_data=None)

    assert "secret ak_key is not set" in caplog.text
    assert created == ["/backup/disk"]


def test_resolved_values_are_scrubbed_from_the_log(state, monkeypatch, caplog):
    _destinations(
        state,
        [_entry("cloud", "s3:b/p", env={"AWS_SECRET_ACCESS_KEY": "sk_key"})],
    )
    secrets = {"cloud_pass": PASSPHRASE, "sk_key": "planted-secret-value"}
    monkeypatch.setattr("core.backup.schedule.get_secret", secrets.get)
    monkeypatch.setattr(
        "core.backup.schedule._latest_snapshot_at",
        lambda repo, passphrase, extra=None: None,
    )

    def create(**kwargs):
        raise BackupError("restic backup failed: bad key planted-secret-value")

    monkeypatch.setattr("core.backup.schedule.create_snapshot", create)

    with caplog.at_level(logging.ERROR):
        run_due(bifrost_data=None)

    assert "bad key" in caplog.text
    assert "planted-secret-value" not in caplog.text
