"""Restarts and the last exit reason (C5 §5.4 "restarts in 24 h, last exit reason")."""

from __future__ import annotations

import pytest

from services.medic.api.history import (
    CRASH_LOOP_RESTARTS,
    CRASH_LOOP_WINDOW_S,
    history_path,
    record_exit,
    record_start,
)

T0 = 1_800_000_000.0


def _beat(state: str, ts: float = T0 - 100) -> dict:
    return {"ts": ts, "started_at": ts - 50, "cycle": 3, "pid": 9, "state": state}


def test_first_start_ever_has_no_restart_and_no_exit(tmp_path) -> None:
    h = record_start(tmp_path, now=T0, previous_beat=None)
    assert h.restarts == () and h.last_exit is None
    assert history_path(tmp_path).stat().st_mode & 0o777 == 0o600


def test_a_recorded_exit_is_the_next_starts_last_exit(tmp_path) -> None:
    record_start(tmp_path, now=T0, previous_beat=None)
    record_exit(tmp_path, now=T0 + 60, code=0)
    h = record_start(tmp_path, now=T0 + 70, previous_beat=_beat("stopped", T0 + 60))
    assert h.last_exit == {"reason": "clean", "at": "2027-01-15T08:01:00Z"}
    assert h.restarts == (T0 + 70,)


def test_an_error_exit_reads_crash(tmp_path) -> None:
    record_start(tmp_path, now=T0, previous_beat=None)
    record_exit(tmp_path, now=T0 + 5, code=1)
    h = record_start(tmp_path, now=T0 + 20, previous_beat=_beat("stopped"))
    assert h.last_exit["reason"] == "crash"


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("stalled", "watchdog_stall"),  # the watchdog exits without a goodbye
        ("crash-looping", "crash"),  # the host-native loop gave up
        ("running", "unknown"),  # SIGKILL, OOM, power: Medic can't tell which
        ("stopped", "clean"),  # a Medic from before this file existed
    ],
)
def test_no_recorded_exit_falls_back_to_the_last_beat(tmp_path, state, reason) -> None:
    record_start(tmp_path, now=T0 - 200, previous_beat=None)
    h = record_start(tmp_path, now=T0, previous_beat=_beat(state, T0 - 100))
    assert h.last_exit == {"reason": reason, "at": "2027-01-15T07:58:20Z"}


def test_crash_looping_beat_with_ts_zero_is_dated_now(tmp_path) -> None:
    h = record_start(tmp_path, now=T0, previous_beat=_beat("crash-looping", 0))
    assert h.last_exit == {"reason": "crash", "at": "2027-01-15T08:00:00Z"}


def test_the_exit_is_read_once(tmp_path) -> None:
    record_start(tmp_path, now=T0, previous_beat=None)
    record_exit(tmp_path, now=T0 + 1, code=0)
    record_start(tmp_path, now=T0 + 2, previous_beat=_beat("stopped", T0 + 1))
    # This run never says goodbye (kill -9): its exit is unknown, not the old one.
    h = record_start(tmp_path, now=T0 + 90, previous_beat=_beat("running", T0 + 80))
    assert h.last_exit["reason"] == "unknown"


def test_restarts_are_kept_for_24_hours_only(tmp_path) -> None:
    record_start(tmp_path, now=T0, previous_beat=None)
    record_start(tmp_path, now=T0 + 10, previous_beat=None)
    h = record_start(tmp_path, now=T0 + 86_400 + 20, previous_beat=None)
    assert h.restarts == (T0 + 86_400 + 20,)


def test_crash_loop_counter(tmp_path) -> None:
    record_start(tmp_path, now=T0, previous_beat=None)
    for i in range(1, CRASH_LOOP_RESTARTS + 2):
        h = record_start(tmp_path, now=T0 + 10 * i, previous_beat=None)
    now = T0 + 10 * (CRASH_LOOP_RESTARTS + 1)
    assert h.restarts_since(now - CRASH_LOOP_WINDOW_S) == CRASH_LOOP_RESTARTS + 1
    assert h.restarts_since(now + 1) == 0


def test_a_corrupt_file_starts_fresh_and_never_raises(tmp_path, caplog) -> None:
    history_path(tmp_path).parent.mkdir(parents=True)
    history_path(tmp_path).write_text("{not json")
    h = record_start(tmp_path, now=T0, previous_beat=_beat("stalled"))
    assert h.last_exit["reason"] == "watchdog_stall"
    assert h.restarts == (T0,)  # a beat proves an earlier run


def test_an_unwritable_dir_never_raises(tmp_path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    h = record_start(blocker, now=T0, previous_beat=None)
    assert h.restarts == ()
    record_exit(blocker, now=T0, code=0)  # no exception
