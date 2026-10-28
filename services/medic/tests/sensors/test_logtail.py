"""The Compose log tail through a fake Docker (list + multiplexed /logs).

The fake keeps each container's log, honours `since` inclusively like Docker, and
frames messages the way the daemon does: one frame per message, a 16 KiB chunk
of a longer one per frame with no trailing newline, a timestamp on every frame.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from services.medic.contracts.fingerprint_ref import MAX_LINE
from services.medic.redact import Redactor
from services.medic.sensors import ReadError, Reading
from services.medic.sensors.logtail import (
    ERRORS_AS_SUCCESS,
    LOG_CAPACITY,
    LOG_SERVICES,
    Line,
    LogStream,
    LogTail,
    ProtocolError,
    Reader,
    line_drafts,
)
from services.medic.tests.sensors.harness import FakeSensor, Rig

MEDIC = Path(__file__).resolve().parents[2]
EXAMPLES = json.loads(
    (MEDIC / "contracts/fixtures/fingerprint/examples.json").read_text()
)
CASES = json.loads((MEDIC / "contracts/fixtures/redaction/cases.json").read_text())[
    "must_redact"
]
T0 = 1_800_000_000  # the FakeClock's wall time: a stream follows from here
NS = 1_000_000_000


def ts_ns(sec: float, ns: int = 0) -> int:
    return int(sec) * NS + ns


def stamp(ns: int) -> bytes:
    """Docker's RFC3339Nano: trailing zeros trimmed."""
    when = datetime.fromtimestamp(ns // NS, UTC).strftime("%Y-%m-%dT%H:%M:%S")
    frac = f"{ns % NS:09d}".rstrip("0")
    return (when + ("." + frac if frac else "") + "Z").encode()


class FakeDocker:
    def __init__(self) -> None:
        self.boxes: dict[str, dict[str, Any]] = {}
        self.list_status = 200
        self.logs_status = 200
        self.since: list[str] = []
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def add(self, service: str, cid: str | None = None, *, tty: bool = False) -> str:
        cid = cid or f"{service}1".replace("-", "")
        self.boxes[cid] = {
            "service": service,
            "name": f"vigil-{service}-1",
            "log": [],
            "gen": 0,
            "waiters": [],
            "tty": tty,
        }
        return cid

    def write(
        self,
        cid: str,
        ns: int,
        message: bytes | str,
        *,
        fd: int = 1,
        chunk: int = 16384,
    ) -> None:
        """Append one log message the way the daemon stores and frames it."""
        box = self.boxes[cid]
        data = message.encode() if isinstance(message, str) else message
        pieces = [data[i : i + chunk] for i in range(0, len(data), chunk)] or [b""]
        for i, piece in enumerate(pieces):
            end = b"\r\n" if box["tty"] else b"\n"
            body = stamp(ns) + b" " + piece + (end if i == len(pieces) - 1 else b"")
            frame = (
                body
                if box["tty"]
                else bytes([fd, 0, 0, 0]) + len(body).to_bytes(4, "big") + body
            )
            box["log"].append((ns, frame))
        for waiter in box["waiters"]:
            waiter.set()

    def drop(self, cid: str) -> None:
        """The container stopped: every open follow stream ends."""
        self.boxes[cid]["gen"] += 1
        for waiter in self.boxes[cid]["waiters"]:
            waiter.set()

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/containers/json":
            if self.list_status != 200:
                return httpx.Response(self.list_status, json={"message": "x"})
            rows = [
                {
                    "Id": cid,
                    "Names": [f"/{box['name']}"],
                    "Labels": {"com.docker.compose.service": box["service"]},
                    "State": "running",
                }
                for cid, box in self.boxes.items()
            ]
            return httpx.Response(200, json=rows)
        cid = path.split("/")[2]
        if self.logs_status != 200:
            return httpx.Response(self.logs_status, json={"message": "x"})
        since = request.url.params["since"]
        self.since.append(since)
        sec, _, frac = since.partition(".")
        floor = int(sec) * NS + int((frac or "0").ljust(9, "0"))
        return httpx.Response(200, content=self._follow(cid, floor))

    async def _follow(self, cid: str, floor: int):
        box = self.boxes[cid]
        gen, sent, wake = box["gen"], 0, asyncio.Event()
        box["waiters"].append(wake)
        try:
            while True:
                while sent < len(box["log"]):
                    ns, frame = box["log"][sent]
                    sent += 1
                    if ns >= floor:
                        yield frame
                if box["gen"] != gen:
                    return
                wake.clear()
                await wake.wait()
        finally:
            box["waiters"].remove(wake)


async def settle() -> None:
    for _ in range(300):
        await asyncio.sleep(0)


async def nap(_: float) -> None:
    await asyncio.sleep(0)  # the reconnect backoff: no real waiting


def build(docker: FakeDocker, **kw: Any) -> tuple[Rig, LogTail]:
    catalog = kw.pop("catalog", frozenset())
    sleep = kw.pop("sleep", nap)
    tail = LogTail(
        "proxy", 8472, catalog=catalog, transport=docker.transport, sleep=sleep
    )
    sensors = [LogStream(tail, s) for s in LOG_SERVICES]
    return Rig(sensors, **kw), tail


async def pump(rig: Rig, advance: float = 0) -> list[dict[str, Any]]:
    before = len(rig.sink.observations)
    await rig.tick(advance)
    await settle()
    rig.pipeline.drain()
    return rig.sink.observations[before:]


def lines(rig: Rig, service: str | None = None) -> list[dict[str, Any]]:
    return [
        o
        for o in rig.sink.of("log")
        if service is None or o["target"]["service"] == service
    ]


def pyjson(message: str, **kw: Any) -> str:
    doc = {
        "level": "INFO",
        "logger": "core.x",
        "message": message,
        "msg_template": message,
    }
    return json.dumps({**doc, **kw})


# -- the line path ----------------------------------------------------------


def _raw(spec: dict[str, Any]) -> bytes:
    if spec["format"] == "text":
        return spec["raw"].encode()
    if spec["format"] == "agent_json":
        keys = {
            "ts": "t",
            "level": spec["level"],
            "logger": spec["logger"],
            "msg": spec["template"],
        }
        return json.dumps(keys).encode()
    doc = {
        k: spec[k]
        for k in ("level", "logger", "message", "exc_type", "exception")
        if k in spec
    }
    return json.dumps({**doc, "msg_template": spec["template"]}).encode()


def _fp(spec: dict[str, Any], catalog: set[tuple[str, str]]) -> dict[str, Any]:
    drafts, _ = line_drafts(
        Line(ts_ns(T0 + 1), _raw(spec)),
        service=spec["service"],
        instance=None,
        shape="compose",
        catalog=catalog,
        redactor=Redactor(),
        observed_at=T0,
    )
    return drafts[0]["log"]


def test_fingerprints_equal_the_frozen_examples() -> None:
    catalog = {tuple(c) for c in EXAMPLES["catalog"]}
    for group in EXAMPLES["same"] + EXAMPLES["differ"]:
        use = catalog if group["use_catalog"] else set()
        got = [_fp(spec, use)["fingerprint"] for spec in group["lines"]]
        want = group["expected"]
        assert got == (want if isinstance(want, list) else [want] * len(got)), group[
            "name"
        ]


def test_catalogue_hit_is_trusted_and_an_equal_fstring_is_not() -> None:
    catalog = {tuple(c) for c in EXAMPLES["catalog"]}
    logger, template = next(iter(catalog))
    spec = {
        "service": "soc-daemon",
        "format": "python_json",
        "level": "WARNING",
        "logger": logger,
        "template": template,
    }
    rendered = _fp(
        {
            **spec,
            "message": "Elastic configuration incomplete (missing: x); skipping polls until it is completed",
        },
        catalog,
    )
    assert (rendered["template_trust"], rendered["fingerprint_basis"]) == (
        "catalog",
        "template",
    )
    assert rendered["args"] == ["Elastic", "x"]
    fstring = _fp({**spec, "message": template}, catalog)
    assert (fstring["template_trust"], fstring["fingerprint_basis"]) == (
        "untrusted",
        "normalized",
    )
    # No catalogue wired yet: nothing is trusted.
    assert _fp({**spec, "message": "m"}, set())["template_trust"] == "untrusted"
    # An agent line is never trusted, even when its (logger, msg) is listed.
    agent = {
        "service": "agent-worker",
        "format": "agent_json",
        "level": "warn",
        "logger": logger,
        "template": template,
    }
    got = _fp(agent, catalog)
    assert (got["level"], got["template_trust"]) == ("WARNING", "untrusted")


# -- the stream -------------------------------------------------------------


async def test_formats_signals_and_one_signal_per_process() -> None:
    docker = FakeDocker()
    for service in ("backend", "soc-daemon", "llm-worker", "agent-worker", "bifrost"):
        docker.add(service)
    rig, tail = build(docker)
    await pump(rig)
    docker.write("backend1", ts_ns(T0 + 1), pyjson("hello", trace_id="a" * 32))
    docker.write(
        "socdaemon1",
        ts_ns(T0 + 1),
        "2026-10-27 10:00:01,123 - services.daemon.poller - ERROR - poll failed",
    )
    docker.write("llmworker1", ts_ns(T0 + 1), pyjson("Job %s failed: %s", level="INFO"))
    docker.write(
        "llmworker1",
        ts_ns(T0 + 2),
        json.dumps(
            {
                "level": "ERROR",
                "logger": "services.worker.jobs",
                "msg_template": "Job %s failed: %s",
                "message": "Job j1 failed: boom",
            }
        ),
    )
    docker.write(
        "agentworker1",
        ts_ns(T0 + 1),
        json.dumps(
            {
                "ts": "x",
                "level": "warn",
                "logger": "agent.run",
                "msg": "run stalled",
                "error_type": "TimeoutError",
            }
        ),
        fd=2,
    )
    docker.write("bifrost1", ts_ns(T0 + 1), "not json at all")
    await pump(rig)
    seen = {(o["target"]["service"], o["signal"]) for o in lines(rig)}
    assert seen == {
        ("backend", "log_backend"),
        ("soc-daemon", "log_soc_daemon"),
        ("llm-worker", "log_llm_worker"),
        ("llm-worker", ERRORS_AS_SUCCESS),
        ("agent-worker", "log_agent_worker"),
        ("bifrost", "log_bifrost"),
    }
    backend = lines(rig, "backend")[0]
    assert backend["log"]["trace_id"] == "a" * 32
    assert backend["target"]["instance"] == "vigil-backend-1"
    daemon = lines(rig, "soc-daemon")[0]["log"]
    assert (daemon["format"], daemon["level"], daemon["logger"]) == (
        "text",
        "ERROR",
        "services.daemon.poller",
    )
    jobs = [o for o in lines(rig, "llm-worker") if o["signal"] == ERRORS_AS_SUCCESS]
    assert [o["log"]["args"] for o in jobs] == [["j1", "boom"]]
    agent = lines(rig, "agent-worker")[0]["log"]
    assert (agent["format"], agent["level"], agent["exc_type"]) == (
        "agent_json",
        "WARNING",
        "TimeoutError",
    )
    assert lines(rig, "bifrost")[0]["log"]["format"] == "text"
    rig.assert_all_valid()
    await tail.aclose()


async def test_inclusive_since_gives_no_duplicate() -> None:
    docker = FakeDocker()
    cid = docker.add("backend")
    rig, tail = build(docker)
    await pump(rig)
    docker.write(cid, ts_ns(T0 + 1), "a")
    docker.write(cid, ts_ns(T0 + 2, 5), "b")
    docker.write(cid, ts_ns(T0 + 2, 5), "c")  # same ns as b: both are at the cursor
    await pump(rig)
    docker.drop(cid)  # the stream ends; the tail reattaches with the cursor
    await settle()
    docker.write(cid, ts_ns(T0 + 3), "d")
    await pump(rig)
    assert [o["log"]["message"] for o in lines(rig)] == ["a", "b", "c", "d"]
    assert docker.since[:2] == [f"{T0}.000000000", f"{T0 + 2}.000000005"]
    assert len(docker.since) >= 2
    rig.assert_all_valid()
    await tail.aclose()


async def test_nothing_lost_across_a_container_restart() -> None:
    docker = FakeDocker()
    cid = docker.add("backend")
    rig, tail = build(docker)
    await pump(rig)
    docker.write(cid, ts_ns(T0 + 1), "before")
    await pump(rig)
    docker.drop(cid)
    docker.write(cid, ts_ns(T0 + 2), "while down")  # written before it reattaches
    docker.write(cid, ts_ns(T0 + 3), "after restart")
    await pump(rig, 30)
    assert [o["log"]["message"] for o in lines(rig)] == [
        "before",
        "while down",
        "after restart",
    ]
    await tail.aclose()


async def test_long_line_is_rejoined_and_capped() -> None:
    docker = FakeDocker()
    cid = docker.add("backend")
    rig, tail = build(docker)
    await pump(rig)
    text = ("abcdefghij" * 4000).encode()  # 40,000 bytes: three frames
    docker.write(cid, ts_ns(T0 + 1), text)
    docker.write(cid, ts_ns(T0 + 2), "next")
    await pump(rig)
    got = lines(rig)
    assert len(got) == 2
    assert got[0]["log"]["message"] == text[:MAX_LINE].decode()
    assert got[0]["log"]["truncated"] is True
    assert got[0]["source_ts"] != got[1]["source_ts"]  # the first chunk's timestamp
    assert "truncated" not in got[1]["log"]
    await tail.aclose()


def test_reader_demuxes_fragmented_frames_tty_and_refuses_garbage() -> None:
    body = stamp(ts_ns(T0)) + b" hello\n" + stamp(ts_ns(T0, 1)) + b" err\n"
    framed = b"".join(
        bytes([fd, 0, 0, 0]) + len(part).to_bytes(4, "big") + part
        for fd, part in (
            (1, body.split(b"\n")[0] + b"\n"),
            (2, body.split(b"\n")[1] + b"\n"),
        )
    )
    reader = Reader()
    got = [line for i in range(len(framed)) for line in reader.feed(framed[i : i + 1])]
    assert [(line.raw, line.ts_ns) for line in got] == [
        (b"hello", ts_ns(T0)),
        (b"err", ts_ns(T0, 1)),
    ]
    # No TTY header: plain `timestamp line` rows.
    assert [line.raw for line in Reader().feed(body)] == [b"hello", b"err"]
    try:
        list(Reader().feed(b"\x01\x07\x00\x00\x00\x00\x00\x01x"))
    except ProtocolError:
        pass
    else:
        raise AssertionError("a bad frame header must be refused")


async def test_tty_container_has_no_frame_header() -> None:
    docker = FakeDocker()
    cid = docker.add("backend", tty=True)
    rig, tail = build(docker)
    await pump(rig)
    docker.write(cid, ts_ns(T0 + 1), "from a tty")
    await pump(rig)
    assert [o["log"]["message"] for o in lines(rig)] == ["from a tty"]
    await tail.aclose()


# -- health -----------------------------------------------------------------


async def test_unreadable_stream_is_an_error_then_blind() -> None:
    docker = FakeDocker()
    docker.add("backend")
    docker.logs_status = 403
    rig, tail = build(docker)
    for _ in range(4):
        await pump(rig, 30)
    states = [h["state"] for h in rig.health("docker.logs.backend")]
    assert states[-1] == "blind"
    errors = [
        o
        for o in rig.sink.observations
        if o["signal"] == "log_backend" and o["outcome"] == "error"
    ]
    assert errors and errors[0]["error"] == {"class": "http_status", "http_status": 403}
    docker.logs_status = 200  # recovers
    docker.write(next(iter(docker.boxes)), ts_ns(T0 + 1), "back")
    for _ in range(2):
        await pump(rig, 30)
    assert rig.health("docker.logs.backend")[-1]["state"] == "ok"
    rig.assert_all_valid()
    await tail.aclose()


async def test_unreachable_proxy_goes_blind_for_every_service() -> None:
    docker = FakeDocker()
    docker.add("backend")
    docker.list_status = 500
    rig, tail = build(docker)
    for _ in range(4):
        await pump(rig, 30)
    for service in LOG_SERVICES:
        assert rig.health(f"docker.logs.{service}")[-1]["state"] == "blind"
    await tail.aclose()


async def test_quiet_stream_is_not_an_error() -> None:
    docker = FakeDocker()
    docker.add("backend")
    rig, tail = build(docker)
    for _ in range(6):
        await pump(rig, 30)
    health = rig.health("docker.logs.backend")
    assert {h["state"] for h in health[1:]} == {"ok"}
    assert health[-1]["consecutive_failures"] == 0
    assert not [o for o in rig.sink.observations if o["signal"] == "log_backend"]
    await tail.aclose()


async def test_absent_optional_service_is_not_installed_never_blind() -> None:
    docker = FakeDocker()
    docker.add("backend")  # no soc-daemon, no llm-worker
    rig, tail = build(docker)
    for _ in range(6):
        await pump(rig, 30)
    for sensor in ("docker.logs.soc-daemon", "docker.logs.llm-worker"):
        last = rig.health(sensor)[-1]
        assert last["state"] == "ok", sensor
        assert last["consecutive_failures"] == 0
        assert last["last_error"] == {"class": "not_installed"}
    absent = [o for o in rig.sink.observations if o["signal"] == "log_soc_daemon"]
    assert absent and all(o["error"] == {"class": "not_installed"} for o in absent)
    rig.assert_all_valid()
    await tail.aclose()


async def test_all_not_installed_cycle_never_counts_toward_blind() -> None:
    sensor = FakeSensor(
        "optional",
        readings=[Reading("api_health", "backend", error=ReadError("not_installed"))],
    )
    rig = Rig([sensor])
    for _ in range(6):
        await rig.tick(30)
    assert rig.health("optional")[-1]["state"] == "ok"
    assert rig.health("optional")[-1]["consecutive_failures"] == 0
    # A different all-error cycle still counts.
    sensor.readings = [Reading("api_health", "backend", error=ReadError("refused"))]
    for _ in range(4):
        await rig.tick(30)
    assert rig.health("optional")[-1]["state"] == "blind"


# -- the buffer and secrets -------------------------------------------------


async def test_over_capacity_drops_the_oldest_and_counts() -> None:
    assert LOG_CAPACITY == 10_000
    docker = FakeDocker()
    cid = docker.add("backend")
    rig, tail = build(docker, log_capacity=5)
    await pump(rig)
    for i in range(8):
        docker.write(cid, ts_ns(T0 + 1, i), f"line {i}")
    await rig.tick()
    await settle()
    await rig.tick(30)  # the next health report carries the count
    await settle()
    rig.pipeline.drain()
    assert [o["log"]["message"] for o in lines(rig)] == [
        f"line {i}" for i in range(3, 8)
    ]
    assert rig.stats["docker.logs.backend"].dropped == 3
    assert rig.health("docker.logs.backend")[-1]["counters"]["dropped"] == 3
    await tail.aclose()


async def test_canary_secrets_never_reach_the_sink_or_medics_own_log(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    docker = FakeDocker()
    cid = docker.add("backend")
    bifrost = docker.add("bifrost")
    rig, tail = build(docker, choke=None)  # the production choke point
    await pump(rig)
    for i, case in enumerate(CASES):
        secret_free = case["input"].replace("\n", " ")
        docker.write(bifrost, ts_ns(T0 + 1, i), "text " + secret_free)
        docker.write(
            cid,
            ts_ns(T0 + 1, i),
            pyjson("m " + case["input"], exception="Traceback\n" + case["input"]),
        )
    await pump(rig)
    # A line that can't be processed is dropped, never logged raw.
    docker.write(cid, ts_ns(T0 + 2), b"\xff\xfe PASSWORD=canaryXYZ12345 \xff")
    await pump(rig)
    assert len(lines(rig)) >= 2 * len(CASES)
    blob = json.dumps(rig.sink.observations) + caplog.text + repr(tail._streams)
    for case in CASES:
        for part in case["secret"].split("\n"):
            assert part.strip() == "" or part not in blob, case["id"]
    assert "canaryXYZ12345" not in blob
    rig.assert_all_valid()
    await tail.aclose()


async def test_stopped_container_backs_off_and_a_lone_surrogate_is_kept() -> None:
    docker = FakeDocker()
    cid = docker.add("backend")
    delays: list[float] = []

    async def record(seconds: float) -> None:
        delays.append(seconds)
        await asyncio.sleep(0)

    rig, tail = build(docker, sleep=record)
    await pump(rig)
    raw = r'{"level":"INFO","logger":"core.x","message":"bad \ud83d","msg_template":"bad \ud83d"}'
    docker.write(cid, ts_ns(T0 + 1), raw)
    await pump(rig)
    assert [o["log"]["message"] for o in lines(rig)] == ["bad ?"]
    assert rig.stats["docker.logs.backend"].redactor_failures == 0
    docker.drop(cid)  # stopped: each reattach replays the cursor line and ends
    for _ in range(6):
        docker.drop(cid)
        await settle()
    assert delays and delays[-1] > 1.0  # backing off, not retrying every second
    assert len(lines(rig)) == 1
    await tail.aclose()
