"""`run`: the master switch (C8 rule 1) and the heartbeat loop (C5 §5.1)."""

from __future__ import annotations

import json
import logging
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
    assert main(["run"], env=env, clock=clock, sleep=clock.sleep, max_cycles=3) == 0

    record = json.loads(heartbeat_path(tmp_path).read_text())
    assert record["cycle"] == 3
    assert record["state"] == "running"
    assert record["ts"] == clock.wall()
    assert clock.wall() - record["started_at"] == 3 * BEAT_INTERVAL_S
    assert check_heartbeat(tmp_path, now=clock.wall())[0]


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
