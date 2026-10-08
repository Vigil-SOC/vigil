"""A time Medic wasn't watching becomes one `gap` record (S2-5 (a); C5 §5.1, C4 §6.8).

The watchdog marks the heartbeat `stalled` before it exits; the next start reads
the last heartbeat (or, with none, the last record) and writes one gap.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

from services.medic.app.cli import main
from services.medic.app.gap import pending_gap, write_gap
from services.medic.app.heartbeat import (
    WATCHDOG_AFTER_S,
    check_heartbeat,
    heartbeat_path,
    write_heartbeat,
)
from services.medic.store import open_writer, verify_store
from services.medic.tests.fakes import FakeClock
from services.medic.tests.store.chains import stored


class Died(BaseException):
    """The process went away mid-cycle (kill -9, OOM, os._exit)."""


def _env(data_dir: Path) -> dict[str, str]:
    # 127.0.0.1:9 (discard) refuses at once: the sensor reads "not ready" quickly.
    return {
        "VIGIL_MEDIC_ENABLED": "true",
        "VIGIL_MEDIC_DATA_DIR": str(data_dir),
        "VIGIL_MEDIC_AGENT_WORKER_ADDR": "127.0.0.1:9",
    }


def _run(data_dir: Path, clock: FakeClock, cycles: int, **kw) -> int:
    return main(
        ["run"],
        env=_env(data_dir),
        clock=clock,
        sleep=kw.pop("sleep", clock.sleep),
        max_cycles=cycles,
        **kw,
    )


def _gaps(data_dir: Path) -> list[dict]:
    return [r for r in stored(data_dir) if r["type"] == "gap"]


def _beat(data_dir: Path) -> dict:
    return json.loads(heartbeat_path(data_dir).read_text())


def _stall(data_dir: Path, clock: FakeClock) -> None:
    """Run until the third sleep, then hang until the real watchdog thread fires."""
    fired = threading.Event()
    codes: list[int] = []
    calls = 0

    def exit_fn(code: int) -> None:
        codes.append(code)
        fired.set()

    async def sleep(seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            await clock.sleep(seconds)
            return
        clock.advance(WATCHDOG_AFTER_S + 1)
        # The loop is "hung" here; only the watchdog thread moves.
        loop = asyncio.get_running_loop()
        if not await loop.run_in_executor(None, fired.wait, 30):
            raise AssertionError("the watchdog never fired")
        raise Died

    with pytest.raises(Died):
        _run(
            data_dir,
            clock,
            None,
            sleep=sleep,
            watchdog_exit=exit_fn,
            watchdog_poll_s=0.01,
        )
    assert codes and codes[0] != 0


def test_first_start_writes_no_gap(tmp_path: Path) -> None:
    assert _run(tmp_path, FakeClock(), 2) == 0
    assert _gaps(tmp_path) == []


def test_a_stall_writes_one_stalled_gap_on_the_next_start(tmp_path: Path) -> None:
    clock = FakeClock()
    _stall(tmp_path, clock)
    beat = _beat(tmp_path)
    assert beat["state"] == "stalled"
    assert not check_heartbeat(tmp_path, now=clock.wall())[0]

    clock.advance(30)  # restarted by Compose / host-native
    restart = clock.wall()
    assert _run(tmp_path, clock, 2) == 0

    (gap,) = _gaps(tmp_path)
    assert gap["body"]["reason"] == "stalled"
    assert gap["body"]["from"] == _iso(beat["ts"])  # the last good beat
    assert gap["body"]["to"] == _iso(restart)
    assert verify_store(tmp_path).ok


def test_a_restart_after_a_clean_stop_writes_one_off_gap(tmp_path: Path) -> None:
    clock = FakeClock()
    assert _run(tmp_path, clock, 2) == 0
    stopped_at = _beat(tmp_path)["ts"]
    clock.advance(3600)
    assert _run(tmp_path, clock, 2) == 0

    (gap,) = _gaps(tmp_path)
    assert gap["body"] == {
        "from": _iso(stopped_at),
        "to": _iso(clock.wall() - 2 * 15),
        "reason": "off",
    }
    assert verify_store(tmp_path).ok


def test_a_kill_reads_as_off_from_the_last_beat(tmp_path: Path) -> None:
    clock = FakeClock()
    calls = 0

    async def die_on_third(seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise Died
        await clock.sleep(seconds)

    with pytest.raises(Died):
        _run(tmp_path, clock, None, sleep=die_on_third)
    last = _beat(tmp_path)
    assert last["state"] == "running"  # nothing got to say goodbye

    clock.advance(120)
    assert _run(tmp_path, clock, 1) == 0
    (gap,) = _gaps(tmp_path)
    assert gap["body"]["reason"] == "off"
    assert gap["body"]["from"] == _iso(last["ts"])
    assert verify_store(tmp_path).ok


def test_each_restart_writes_exactly_one_gap(tmp_path: Path) -> None:
    clock = FakeClock()
    for _ in range(3):
        assert _run(tmp_path, clock, 1) == 0
        clock.advance(600)
    assert [g["body"]["reason"] for g in _gaps(tmp_path)] == ["off", "off"]
    assert verify_store(tmp_path).ok


def test_a_crash_before_the_new_beat_doesnt_write_the_gap_twice(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    assert _run(tmp_path, clock, 1) == 0
    clock.advance(600)
    for _ in range(2):  # gap written, then death before the start-up beat
        gap = pending_gap(tmp_path, now=clock.wall())
        with open_writer(tmp_path) as writer:
            write_gap(writer, gap)
        clock.advance(5)
    assert len(_gaps(tmp_path)) == 1


def test_no_heartbeat_but_records_uses_the_last_record(tmp_path: Path) -> None:
    clock = FakeClock()
    assert _run(tmp_path, clock, 1) == 0
    with open_writer(tmp_path) as writer:
        writer.append(
            {
                "v": 1,
                "at": "2027-01-15T08:00:00Z",
                "type": "gap",
                "body": {
                    "from": "2027-01-15T07:00:00Z",
                    "to": "2027-01-15T08:00:00Z",
                    "reason": "off",
                },
            }
        )
    heartbeat_path(tmp_path).unlink()
    gap = pending_gap(tmp_path, now=clock.wall() + 600)
    assert gap is not None
    assert gap["from"] == "2027-01-15T08:00:00Z" and gap["reason"] == "off"


def test_a_beat_from_the_future_doesnt_break_the_chain(tmp_path: Path) -> None:
    # The clock jumped back: a gap that ended before it started would fail verify().
    clock = FakeClock()
    assert _run(tmp_path, clock, 1) == 0
    beat = _beat(tmp_path)
    write_heartbeat(
        tmp_path,
        cycle=beat["cycle"],
        now=clock.wall() + 3600,
        started_at=beat["started_at"],
        pid=beat["pid"],
        state="stopped",
    )
    assert _run(tmp_path, clock, 1) == 0
    (gap,) = _gaps(tmp_path)
    assert gap["body"]["from"] == gap["body"]["to"]
    assert verify_store(tmp_path).ok


def _iso(ts: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(int(ts), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
