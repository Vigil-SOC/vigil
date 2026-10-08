"""`python -m services.medic run` end to end: a fault becomes an incident in the log.

A local stub plays the agent worker's `/readyz`. Medic runs its real path: the
`http_ready` sensor, the K2 choke, the engine on the 15 s tick, router R1 and the
store, on a fake clock. The stub's body and headers carry a canary secret that
must never reach the store or Medic's logs.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from services.medic.app.cli import main
from services.medic.app.wiring import TICK_S
from services.medic.store import verify_store
from services.medic.tests.fakes import FakeClock
from services.medic.tests.store.chains import stored

CANARY = "canaryS4readyz7Q2x9LmT"
RULE = "pipeline.agent-worker-not-ready"


class Stub:
    """An agent worker whose readiness the test flips."""

    def __init__(self) -> None:
        self.ready = True
        self.hits = 0
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                stub.hits += 1
                body = b"ready" if stub.ready else f"not ready {CANARY}".encode()
                self.send_response(200 if stub.ready else 503)
                self.send_header("Content-Type", "text/plain")
                self.send_header("X-Debug", f"redis password={CANARY}")
                self.send_header("Set-Cookie", f"session={CANARY}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def addr(self) -> str:
        return f"127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub() -> Iterator[Stub]:
    s = Stub()
    yield s
    s.close()


async def _sensors_idle(timeout: float = 5.0) -> None:
    """Let real I/O finish before the fake clock moves: a sensor still in flight
    when 15 fake seconds pass would be cut as a timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        busy = [
            t
            for t in asyncio.all_tasks()
            if t.get_name().startswith("medic.sensor.") and not t.done()
        ]
        if not busy:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("a sensor call never finished")


def _sleeper(
    clock: FakeClock, script: Callable[[float], None]
) -> Callable[[float], object]:
    """The loop's sleep: finish real I/O, let the test act, then advance time."""
    started = clock.wall()

    async def sleep(seconds: float) -> None:
        await _sensors_idle()
        script(clock.wall() - started)
        clock.advance(seconds)

    return sleep


def _env(data_dir: Path, stub: Stub) -> dict[str, str]:
    return {
        "VIGIL_MEDIC_ENABLED": "true",
        "VIGIL_MEDIC_DATA_DIR": str(data_dir),
        "VIGIL_MEDIC_INSTALL_SHAPE": "compose",
        "VIGIL_MEDIC_AGENT_WORKER_ADDR": stub.addr,
    }


def _outage(stub: Stub, down_from: float, up_at: float) -> Callable[[float], None]:
    def script(elapsed: float) -> None:
        stub.ready = not (down_from <= elapsed < up_at)

    return script


def _run(data_dir: Path, stub: Stub, clock: FakeClock, script, cycles: int) -> int:
    return main(
        ["run"],
        env=_env(data_dir, stub),
        clock=clock,
        sleep=_sleeper(clock, script),
        max_cycles=cycles,
    )


def _incident_records(data_dir: Path) -> list[dict]:
    return [r for r in stored(data_dir) if r["type"].startswith("incident_")]


def test_readyz_failure_opens_after_the_hold_and_resolves_after_recovery(
    tmp_path: Path, stub: Stub
) -> None:
    clock = FakeClock()
    t0 = clock.wall()
    down_from, up_at = 60, 420
    assert _run(tmp_path, stub, clock, _outage(stub, down_from, up_at), 60) == 0

    by_type = {r["type"]: r for r in _incident_records(tmp_path)}
    opened = by_type["incident_opened"]
    assert opened["body"]["rule"]["id"] == RULE
    assert opened["body"]["lane"] == {"value": 3, "reason": "rule"}
    assert opened["body"]["install_shape"] == "compose"

    def offset(ts: str) -> float:
        from datetime import datetime

        return datetime.fromisoformat(ts).timestamp() - t0

    # Not before the 2 m hold has run from the first not-ready read...
    since = offset(opened["body"]["active_since"])
    assert down_from <= since < down_from + 2 * TICK_S + 30
    assert offset(opened["at"]) - since >= 120
    # ...resolved only after recovery plus keep_firing_for (5 m).
    resolved = by_type["incident_resolved"]
    assert resolved["body"]["incident_id"] == opened["body"]["incident_id"]
    assert resolved["body"]["how"] == "cleared"
    assert offset(resolved["at"]) >= up_at + 300
    assert len([r for r in stored(tmp_path) if r["type"] == "incident_opened"]) == 1

    report = verify_store(tmp_path)
    assert report.ok and report.count == len(stored(tmp_path))
    assert stub.hits >= 10


def test_a_short_blip_opens_nothing(tmp_path: Path, stub: Stub) -> None:
    clock = FakeClock()
    assert _run(tmp_path, stub, clock, _outage(stub, 60, 120), 20) == 0
    assert _incident_records(tmp_path) == []
    assert verify_store(tmp_path).ok


def test_the_canary_never_reaches_the_store_or_the_logs(
    tmp_path: Path, stub: Stub, caplog
) -> None:
    clock = FakeClock()
    with caplog.at_level(logging.DEBUG):
        assert _run(tmp_path, stub, clock, _outage(stub, 0, 10_000), 20) == 0
    assert any(r["type"] == "incident_opened" for r in stored(tmp_path))
    assert CANARY not in caplog.text
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert CANARY.encode() not in path.read_bytes(), path


def test_a_restart_carries_an_open_incident_to_its_resolution(
    tmp_path: Path, stub: Stub
) -> None:
    clock = FakeClock()
    assert _run(tmp_path, stub, clock, _outage(stub, 0, 10_000), 20) == 0
    (opened,) = [r for r in stored(tmp_path) if r["type"] == "incident_opened"]

    clock.advance(60)  # off for a minute, then back with a healthy worker
    assert _run(tmp_path, stub, clock, _outage(stub, 0, 0), 40) == 0

    records = stored(tmp_path)
    assert [r["type"] for r in records].count("incident_opened") == 1
    (resolved,) = [r for r in records if r["type"] == "incident_resolved"]
    assert resolved["body"]["incident_id"] == opened["body"]["incident_id"]
    assert verify_store(tmp_path).ok


def test_a_recording_replays_to_the_same_engine_records(
    tmp_path: Path, stub: Stub
) -> None:
    """What the live store holds for an incident, a replay of the observations
    the engine saw reproduces byte for byte (A3-5)."""
    from services.medic.app import wiring
    from services.medic.store.files import INSTANCE_NAME
    from services.medic.tests.app.replay import replay

    seen: list[dict] = []
    arrived: list[float] = []
    original = wiring.EngineSink.write
    clock = FakeClock()

    def tap(self, observation) -> None:
        # A live observation reaches the engine on the cycle after its read
        # finishes, so the recording keeps when it arrived, not only when it was read.
        seen.append(dict(observation))
        arrived.append(clock.wall())
        original(self, observation)

    start = clock.wall()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wiring.EngineSink, "write", tap)
        assert _run(tmp_path / "live", stub, clock, _outage(stub, 30, 400), 60) == 0
    live = [r for r in stored(tmp_path / "live") if r["type"].startswith("incident_")]
    assert live

    from datetime import UTC, datetime

    instance = (tmp_path / "live" / INSTANCE_NAME).read_text().strip()
    replay(
        seen,
        wiring.load_dev_rules(),
        tmp_path / "replay",
        arrived=arrived,
        instance_id=instance,
        start=datetime.fromtimestamp(start, UTC),
        seconds=int(clock.wall() - start),
    )
    again = stored(tmp_path / "replay")
    strip = ("seq", "prev", "hash")
    assert [{k: v for k, v in r.items() if k not in strip} for r in again] == [
        {k: v for k, v in r.items() if k not in strip} for r in live
    ]


def test_medics_own_log_is_redacted_while_it_runs(
    tmp_path: Path, stub: Stub, caplog
) -> None:
    # S3 → S4: the record factory is installed before anything logs.
    clock = FakeClock()

    def script(elapsed: float) -> None:
        logging.getLogger("services.medic").warning("read password=%s", CANARY)

    with caplog.at_level(logging.DEBUG):
        assert _run(tmp_path, stub, clock, script, 2) == 0
    assert "read password=" in caplog.text
    assert CANARY not in caplog.text


async def test_what_a_sensor_reads_passes_the_k2_choke_before_the_engine(
    tmp_path: Path,
) -> None:
    """A sensor that does read a secret (unlike http_ready) still can't get it
    past the wiring: the pipeline's default choke redacts before the engine."""
    import json as _json

    from services.medic.app.wiring import Medic, load_dev_rules
    from services.medic.sensors import Reading, Value
    from services.medic.store import open_writer
    from services.medic.tests.sensors.harness import FakeSensor

    leaky = FakeSensor(
        "canary.readyz",
        service="agent-worker",
        covers=("agent_worker_readyz",),
        readings=[
            Reading(
                "agent_worker_readyz",
                "agent-worker",
                values=[
                    Value.flag("ready", False),
                    Value.text("detail", f"not ready password={CANARY}"),
                ],
            )
        ],
    )
    seen: list[dict] = []
    clock = FakeClock()
    with open_writer(tmp_path) as writer:
        medic = Medic(
            writer=writer,
            rules=load_dev_rules(),
            sensors=[leaky],
            clock=clock,
            shape="compose",
        )
        original = medic.sink.write
        medic.sink.write = lambda o: (seen.append(dict(o)), original(o))
        for _ in range(20):
            await medic.cycle()
            await _sensors_idle()
            clock.advance(TICK_S)
    assert any(o["kind"] == "sample" for o in seen)
    assert CANARY not in _json.dumps(seen)
    assert any(r["type"] == "incident_opened" for r in stored(tmp_path))
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert CANARY.encode() not in path.read_bytes(), path
