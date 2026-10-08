"""The watchdog turns a hung main loop into an exit (C5 §5.1: by 180 s)."""

from __future__ import annotations

import logging
import threading

from services.medic.app.heartbeat import WATCHDOG_AFTER_S
from services.medic.app.watchdog import Watchdog
from services.medic.tests.fakes import FakeClock


class Exits:
    def __init__(self) -> None:
        self.codes: list[int] = []
        self.called = threading.Event()

    def __call__(self, code: int) -> None:
        self.codes.append(code)
        self.called.set()


def test_does_not_fire_while_the_loop_beats() -> None:
    clock, exits = FakeClock(), Exits()
    dog = Watchdog(monotonic=clock.monotonic, exit_fn=exits)
    for _ in range(20):
        clock.advance(30)
        dog.beat()
        assert not dog.check()
    assert exits.codes == []


def test_fires_on_a_stalled_loop(caplog) -> None:
    clock, exits = FakeClock(), Exits()
    dog = Watchdog(monotonic=clock.monotonic, exit_fn=exits)
    clock.advance(WATCHDOG_AFTER_S)
    assert not dog.check()
    clock.advance(1)
    with caplog.at_level(logging.ERROR, logger="services.medic"):
        assert dog.check()
    assert exits.codes and exits.codes[0] != 0
    assert "stalled" in caplog.text
    # The log carries the main thread's real stack (here: this test function).
    assert "test_fires_on_a_stalled_loop" in caplog.text


def test_fires_from_its_own_thread() -> None:
    # The real failure is a main loop that never returns, so only a thread can see it.
    clock, exits = FakeClock(), Exits()
    dog = Watchdog(monotonic=clock.monotonic, exit_fn=exits, poll_s=0.01)
    dog.start()
    try:
        clock.advance(WATCHDOG_AFTER_S + 1)
        assert exits.called.wait(timeout=5)
    finally:
        dog.stop()
    assert exits.codes[0] != 0
