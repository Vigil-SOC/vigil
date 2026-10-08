"""`run`: the master switch (C8 rule 1) and the heartbeat loop (C5 §5.1)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from services.medic.app.cli import main
from services.medic.app.config import default_data_dir, is_enabled
from services.medic.app.heartbeat import (
    BEAT_INTERVAL_S,
    check_heartbeat,
    heartbeat_path,
)
from services.medic.app.wiring import TICK_S
from services.medic.tests.fakes import FakeClock

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "value", [None, "", "false", "False", "0", "no", "off", "ture"]
)
def test_flag_off_values(value: str | None) -> None:
    env = {} if value is None else {"VIGIL_MEDIC_ENABLED": value}
    assert not is_enabled(env)


@pytest.mark.parametrize("value", ["true", "TRUE", " true ", "1", "yes", "on"])
def test_flag_on_values(value: str) -> None:
    assert is_enabled({"VIGIL_MEDIC_ENABLED": value})


def test_flag_off_run_exits_with_a_message(tmp_path: Path, caplog) -> None:
    env = {"VIGIL_MEDIC_DATA_DIR": str(tmp_path)}
    with caplog.at_level(logging.INFO, logger="services.medic"):
        assert main(["run"], env=env) == 0
    assert "VIGIL_MEDIC_ENABLED" in caplog.text
    assert "off" in caplog.text.lower()
    assert not heartbeat_path(tmp_path).exists()


def test_unrecognised_flag_value_is_named(tmp_path: Path, caplog) -> None:
    env = {"VIGIL_MEDIC_ENABLED": "ture", "VIGIL_MEDIC_DATA_DIR": str(tmp_path)}
    with caplog.at_level(logging.INFO, logger="services.medic"):
        assert main(["run"], env=env) == 0
    assert "'ture'" in caplog.text


def test_flag_on_heartbeat_appears(tmp_path: Path) -> None:
    clock = FakeClock()
    env = {"VIGIL_MEDIC_ENABLED": "true", "VIGIL_MEDIC_DATA_DIR": str(tmp_path)}
    seen: list[tuple[dict, bool]] = []

    async def sleep_and_look(seconds: float) -> None:
        # What `check` sees while Medic runs: the beat the last cycle wrote.
        record = json.loads(heartbeat_path(tmp_path).read_text())
        seen.append((record, check_heartbeat(tmp_path, now=clock.wall())[0]))
        await clock.sleep(seconds)

    assert main(["run"], env=env, clock=clock, sleep=sleep_and_look, max_cycles=3) == 0

    # A cycle runs, then beats, then sleeps one 15 s tick (S4).
    assert [r["cycle"] for r, _ in seen] == [1, 2, 3]
    assert all(r["state"] == "running" and fresh for r, fresh in seen)
    last = json.loads(heartbeat_path(tmp_path).read_text())
    assert last["cycle"] == 3
    assert last["ts"] - last["started_at"] == 3 * TICK_S
    assert TICK_S <= BEAT_INTERVAL_S  # C5: a beat at least every 30 s


def test_clean_stop_marks_the_beat_stopped(tmp_path: Path) -> None:
    # After a stop, `check` must not report a live Medic for the next 120 s.
    clock = FakeClock()
    env = {"VIGIL_MEDIC_ENABLED": "true", "VIGIL_MEDIC_DATA_DIR": str(tmp_path)}
    assert main(["run"], env=env, clock=clock, sleep=clock.sleep, max_cycles=2) == 0
    assert json.loads(heartbeat_path(tmp_path).read_text())["state"] == "stopped"
    assert not check_heartbeat(tmp_path, now=clock.wall())[0]


def test_run_restores_the_umask(tmp_path: Path) -> None:
    clock = FakeClock()
    env = {"VIGIL_MEDIC_ENABLED": "true", "VIGIL_MEDIC_DATA_DIR": str(tmp_path)}
    before = os.umask(0o022)
    try:
        main(["run"], env=env, clock=clock, sleep=clock.sleep, max_cycles=0)
        assert os.umask(0o022) == 0o022
    finally:
        os.umask(before)


def test_beat_is_written_at_start(tmp_path: Path) -> None:
    # Before the first cycle completes, `check` must see a start-up beat, not nothing.
    clock = FakeClock()
    env = {"VIGIL_MEDIC_ENABLED": "true", "VIGIL_MEDIC_DATA_DIR": str(tmp_path)}
    assert main(["run"], env=env, clock=clock, sleep=clock.sleep, max_cycles=0) == 0
    assert json.loads(heartbeat_path(tmp_path).read_text())["cycle"] == 0


def test_run_creates_a_private_data_dir(tmp_path: Path) -> None:
    clock = FakeClock()
    data = tmp_path / "medic"
    env = {"VIGIL_MEDIC_ENABLED": "true", "VIGIL_MEDIC_DATA_DIR": str(data)}
    assert main(["run"], env=env, clock=clock, sleep=clock.sleep, max_cycles=1) == 0
    assert data.stat().st_mode & 0o777 == 0o700


def test_unwritable_data_dir_fails_loudly(tmp_path: Path, caplog) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    env = {"VIGIL_MEDIC_ENABLED": "true", "VIGIL_MEDIC_DATA_DIR": str(blocker / "x")}
    with caplog.at_level(logging.ERROR, logger="services.medic"):
        assert main(["run"], env=env, max_cycles=0) == 1
    assert "data dir" in caplog.text


def test_default_data_dir_per_platform() -> None:
    assert default_data_dir("linux") == Path("/var/lib/vigil-medic")
    assert default_data_dir("darwin") == Path(
        "/Library/Application Support/vigil-medic"
    )


def test_usage_error() -> None:
    assert main([], env={}) == 2
    assert main(["frobnicate"], env={}) == 2


def _module(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "services.medic", *args],
        cwd=REPO_ROOT,
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_module_entry_point_flag_off(tmp_path: Path) -> None:
    result = _module("run", env={"VIGIL_MEDIC_DATA_DIR": str(tmp_path)})
    assert result.returncode == 0
    assert "VIGIL_MEDIC_ENABLED" in result.stderr


def test_module_entry_point_check_without_heartbeat(tmp_path: Path) -> None:
    result = _module("check", env={"VIGIL_MEDIC_DATA_DIR": str(tmp_path)})
    assert result.returncode == 1
    assert "no heartbeat" in result.stdout


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("VIGIL_MEDIC_INSTALL_SHAPE", "kubernetes"),
        ("VIGIL_MEDIC_AGENT_WORKER_ADDR", "http://agent-worker:6990/readyz"),
        ("VIGIL_MEDIC_AGENT_WORKER_ADDR", "user:hunter2secret@agent-worker:6990"),
        ("VIGIL_MEDIC_AGENT_WORKER_ADDR", "agent-worker:70000"),
    ],
)
def test_a_setting_medic_cant_use_stops_it_without_echoing_the_value(
    tmp_path: Path, caplog, var: str, value: str
) -> None:
    env = {
        "VIGIL_MEDIC_ENABLED": "true",
        "VIGIL_MEDIC_DATA_DIR": str(tmp_path),
        var: value,
    }
    with caplog.at_level(logging.INFO, logger="services.medic"):
        assert main(["run"], env=env, max_cycles=0) == 1
    assert var in caplog.text
    assert value not in caplog.text
    assert not heartbeat_path(tmp_path).exists()


def test_check_reports_a_stalled_medic_unhealthy(tmp_path: Path) -> None:
    from services.medic.app.heartbeat import mark_stalled, write_heartbeat

    write_heartbeat(tmp_path, cycle=4, now=1000.0, started_at=900.0, pid=1)
    mark_stalled(tmp_path)
    ok, reason = check_heartbeat(tmp_path, now=1001.0)
    assert not ok and "stalled" in reason
    # The last good beat is kept: the next start's gap starts there.
    assert json.loads(heartbeat_path(tmp_path).read_text())["ts"] == 1000.0
