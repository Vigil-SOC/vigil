"""`check` passes only on a fresh heartbeat (C5 §5.1: stale after 120 s, 300 s in start-up grace)."""

from __future__ import annotations

from pathlib import Path

from services.medic.app.cli import main
from services.medic.app.heartbeat import (
    check_heartbeat,
    heartbeat_path,
    write_heartbeat,
)

T0 = 1_800_000_000.0


def beat(data_dir: Path, *, at: float, started_at: float, cycle: int = 3) -> None:
    write_heartbeat(data_dir, cycle=cycle, now=at, started_at=started_at, pid=4242)


def test_no_heartbeat_fails(tmp_path: Path) -> None:
    ok, reason = check_heartbeat(tmp_path, now=T0)
    assert not ok
    assert "no heartbeat" in reason


def test_fresh_heartbeat_passes(tmp_path: Path) -> None:
    beat(tmp_path, at=T0 - 30, started_at=T0 - 3600)
    ok, reason = check_heartbeat(tmp_path, now=T0)
    assert ok, reason


def test_heartbeat_at_the_limit_passes(tmp_path: Path) -> None:
    beat(tmp_path, at=T0 - 120, started_at=T0 - 3600)
    assert check_heartbeat(tmp_path, now=T0)[0]


def test_stale_heartbeat_fails(tmp_path: Path) -> None:
    beat(tmp_path, at=T0 - 121, started_at=T0 - 3600)
    ok, reason = check_heartbeat(tmp_path, now=T0)
    assert not ok
    assert "stale" in reason and "121" in reason


def test_start_up_grace_allows_300_s(tmp_path: Path) -> None:
    # Started 250 s ago, first cycle not yet finished: the start-time beat is 250 s old.
    beat(tmp_path, at=T0 - 250, started_at=T0 - 250, cycle=0)
    assert check_heartbeat(tmp_path, now=T0)[0]


def test_grace_ends_after_300_s(tmp_path: Path) -> None:
    beat(tmp_path, at=T0 - 301, started_at=T0 - 301, cycle=0)
    assert not check_heartbeat(tmp_path, now=T0)[0]


def test_heartbeat_from_the_future_fails(tmp_path: Path) -> None:
    # A clock far ahead would otherwise read as fresh forever.
    beat(tmp_path, at=T0 + 3600, started_at=T0 - 3600)
    ok, reason = check_heartbeat(tmp_path, now=T0)
    assert not ok
    assert "future" in reason


def test_small_clock_step_back_still_passes(tmp_path: Path) -> None:
    beat(tmp_path, at=T0 + 5, started_at=T0 - 3600)
    assert check_heartbeat(tmp_path, now=T0)[0]


def test_unreadable_heartbeat_fails(tmp_path: Path) -> None:
    path = heartbeat_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    ok, reason = check_heartbeat(tmp_path, now=T0)
    assert not ok
    assert "unreadable" in reason


def test_heartbeat_file_is_private(tmp_path: Path) -> None:
    beat(tmp_path, at=T0, started_at=T0)
    assert heartbeat_path(tmp_path).stat().st_mode & 0o777 == 0o600
    assert heartbeat_path(tmp_path).parent.stat().st_mode & 0o777 == 0o700


def test_write_leaves_no_temp_file(tmp_path: Path) -> None:
    beat(tmp_path, at=T0, started_at=T0)
    beat(tmp_path, at=T0 + 30, started_at=T0, cycle=4)
    assert [p.name for p in heartbeat_path(tmp_path).parent.iterdir()] == ["heartbeat"]


def test_check_command_exit_codes(tmp_path: Path, capsys) -> None:
    env = {"VIGIL_MEDIC_DATA_DIR": str(tmp_path)}
    assert main(["check"], env=env, now=T0) == 1
    assert "no heartbeat" in capsys.readouterr().out

    beat(tmp_path, at=T0 - 200, started_at=T0 - 3600)
    assert main(["check"], env=env, now=T0) == 1
    assert "stale" in capsys.readouterr().out

    beat(tmp_path, at=T0 - 10, started_at=T0 - 3600)
    assert main(["check"], env=env, now=T0) == 0


def test_nan_timestamp_fails(tmp_path: Path) -> None:
    # json accepts NaN, and every comparison with NaN is False: it would read as fresh.
    path = heartbeat_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"v": 1, "cycle": 1, "ts": NaN, "started_at": 0, "pid": 1}')
    ok, reason = check_heartbeat(tmp_path, now=T0)
    assert not ok
    assert "unreadable" in reason


def test_stopped_heartbeat_fails(tmp_path: Path) -> None:
    write_heartbeat(
        tmp_path, cycle=5, now=T0, started_at=T0 - 3600, pid=1, state="stopped"
    )
    ok, reason = check_heartbeat(tmp_path, now=T0)
    assert not ok
    assert "stopped" in reason


def test_degraded_heartbeat_passes(tmp_path: Path) -> None:
    # C5 K-d: dependency faults make Medic degraded, never a failed check.
    write_heartbeat(
        tmp_path, cycle=5, now=T0, started_at=T0 - 3600, pid=1, state="degraded"
    )
    assert check_heartbeat(tmp_path, now=T0)[0]


def test_loose_run_dir_is_tightened(tmp_path: Path) -> None:
    run_dir = heartbeat_path(tmp_path).parent
    run_dir.mkdir(mode=0o755)
    run_dir.chmod(0o755)
    beat(tmp_path, at=T0, started_at=T0)
    assert run_dir.stat().st_mode & 0o777 == 0o700
