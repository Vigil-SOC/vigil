"""Trace capture (D7): the observations the engine receives, written as a trace.

`VIGIL_MEDIC_TRACE=<trace-id>` writes `<data dir>/traces/<trace-id>/` in the trace
format v1 layout (`contracts/trace_check.py`). It is a tee behind the engine: it runs
after `Engine.observe`, so it can't block or change detection, and its own failure
stops capture only. Observations arrive already redacted, so a trace holds no more
than the store does.

Capture never resumes: a gap mid-trace would make replay answer wrongly. It stops at
`TRACE_CAP_BYTES` or when the store's free-space floor trips, and writes its manifest
then (`interrupted: false`). A kill leaves no manifest; the next start finalises it
(`recover`) with `interrupted: true`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tomllib
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from services.medic.app.gap import iso
from services.medic.contracts.trace_check import MANIFEST, validate

log = logging.getLogger("services.medic")

TRACE_CAP_BYTES = 256 * 2**20  # per trace, across its files (D10)
TRACES_DIR = "traces"
OBSERVATIONS = "observations"
_TRACE_ID = re.compile(MANIFEST.schema["$defs"]["trace_id"]["pattern"])
_SENSOR_FILE = re.compile(
    MANIFEST.schema["properties"]["sources"]["items"]["properties"]["file"]["pattern"]
)
PACK_ID = "dev"  # the app runs the dev rules, not a signed pack


def medic_version() -> str:
    try:
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        return tomllib.loads(pyproject.read_text())["project"]["version"]
    except (OSError, KeyError, ValueError):
        return "0.0.0"


def _medic_block(shape: str, instance_id: str) -> dict[str, Any]:
    version = medic_version()
    return {
        "version": version,
        "install_shape": shape,
        "instance_id": instance_id,
        "pack": {"id": PACK_ID, "version": version},
    }


def _write_manifest(
    trace_dir: Path,
    medic: dict[str, Any],
    sources: list[dict[str, Any]],
    started_at: str,
    ended_at: str,
    interrupted: bool,
) -> None:
    manifest = {
        "schema_version": "1",
        "trace_id": trace_dir.name,
        "provenance": "captured",
        "medic": medic,
        "capture": {
            "started_at": started_at,
            "ended_at": ended_at,
            "interrupted": interrupted,
        },
        "sources": sources,
    }
    tmp = trace_dir / ".manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, trace_dir / "manifest.json")


def _trim(path: Path) -> bytes:
    """The file's bytes up to its last newline: a failed or killed write can leave a
    torn last line, which the validator counts as a line."""
    data = path.read_bytes()
    keep = data[: data.rfind(b"\n") + 1]
    if len(keep) != len(data):
        os.truncate(path, len(keep))
    return keep


def _source(path: Path) -> dict[str, Any]:
    """The manifest entry for one observations file, from what's on disk."""
    data = path.read_bytes()
    return {
        "sensor": path.stem,
        "file": f"{OBSERVATIONS}/{path.name}",
        "sha256": hashlib.sha256(data).hexdigest(),
        "lines": data.count(b"\n"),
    }


class TraceCapture:
    """Built `off`; `start_capture` returns it `capturing` when there is a trace to write."""

    def __init__(self) -> None:
        self.id: str | None = None
        self.state = "off"  # off | capturing | stopped_at_cap
        self._files: dict[str, BinaryIO] = {}
        self._seq: dict[str, int] = {}
        self._bytes = 0
        self._dir: Path
        self._medic: dict[str, Any]
        self._started = 0
        self._first: int | None = None
        self._last = 0

    def _begin(self, trace_dir: Path, medic: dict[str, Any], tick: int) -> None:
        self._dir, self._medic, self._started = trace_dir, medic, tick
        self.id, self.state = trace_dir.name, "capturing"

    def status(self) -> dict[str, Any]:
        return {"id": self.id, "state": self.state}

    def write(self, observation: Mapping[str, Any], arrived: int) -> None:
        """One line to `observations/<sensor.id>.jsonl`, flushed. Never raises."""
        if self.state != "capturing":
            return
        try:
            sensor = observation["sensor"]["id"]
            seq = self._seq.get(sensor, 0)
            line = (
                json.dumps(
                    {"seq": seq, "arrived": iso(arrived), "observation": observation},
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
                + b"\n"
            )
            if self._bytes + len(line) > TRACE_CAP_BYTES:
                log.warning("Trace capture reached its size cap: stopped for this run")
                self._finish("stopped_at_cap", interrupted=False, now=arrived)
                return
            f = self._files.get(sensor)
            if f is None:
                name = f"{OBSERVATIONS}/{sensor}.jsonl"
                if not _SENSOR_FILE.fullmatch(name):
                    raise ValueError("sensor id is not a trace file name")
                path = self._dir / name
                f = self._files[sensor] = open(path, "xb")  # noqa: SIM115
            f.write(line)
            f.flush()  # a kill loses little
            self._bytes += len(line)
            self._seq[sensor] = seq + 1
            if self._first is None:
                self._first = arrived
            self._last = max(self._last, arrived)
        except Exception as exc:  # noqa: BLE001 (capture never touches detection)
            log.error("Trace capture failed and is stopped: %s", type(exc).__name__)
            self._finish("off", interrupted=True, now=arrived)

    def check_disk(self, stop_reason: str | None) -> None:
        """The store's free-space floor tripped: the volume is shared, so stop."""
        if self.state == "capturing" and stop_reason == "disk":
            log.warning("Trace capture stopped: the free-space floor tripped")
            self._finish("stopped_at_cap", interrupted=False, now=self._last)

    def close(self, now: float) -> None:
        """Clean shutdown: write the manifest."""
        self._finish("off", interrupted=False, now=int(now))

    def _finish(self, state: str, *, interrupted: bool, now: int) -> None:
        """Close the files and write the manifest; no observation, no trace."""
        was_open, self.state = self.state == "capturing", state
        if state == "off":
            self.id = None  # status: id is null when off
        for f in self._files.values():
            try:
                f.close()
            except OSError:
                pass
        self._files.clear()
        if not was_open:
            return
        try:
            if self._first is None:  # nothing written: an empty directory goes
                (self._dir / OBSERVATIONS).rmdir()
                self._dir.rmdir()
                self.id = None
                return
            started = min(self._started, self._first)
            ended = max(now, self._last, started + 1)
            paths = sorted((self._dir / OBSERVATIONS).glob("*.jsonl"))
            for p in paths:
                _trim(p)
            sources = [_source(p) for p in paths]
            _write_manifest(
                self._dir, self._medic, sources, iso(started), iso(ended), interrupted
            )
        except Exception as exc:  # noqa: BLE001
            # No manifest: the next start finalises it from the files.
            log.error(
                "Trace capture couldn't write its manifest: %s", type(exc).__name__
            )


def start_capture(
    data_dir: Path,
    trace_id: str | None,
    *,
    shape: str,
    instance_id: str,
    tick: int,
) -> TraceCapture:
    """Finalise any torn trace from a past run, then open `trace_id` if one is set.
    Never raises: a trace that can't start is logged and Medic runs without it."""
    capture = TraceCapture()
    medic = _medic_block(shape, instance_id)
    traces = data_dir / TRACES_DIR
    try:
        recover(traces, medic)
    except Exception as exc:  # noqa: BLE001
        log.error("Traces not finalised: %s", type(exc).__name__)
    if trace_id is None:
        return capture
    if not _TRACE_ID.fullmatch(trace_id):
        # Not echoed: it never becomes a path.
        log.error("Trace capture is off: VIGIL_MEDIC_TRACE is not a valid trace id")
        return capture
    try:
        traces.mkdir(mode=0o700, exist_ok=True)
        trace_dir = traces / trace_id
        trace_dir.mkdir(mode=0o700)  # atomic: never reuses a directory
        (trace_dir / OBSERVATIONS).mkdir(mode=0o700)
    except FileExistsError:
        log.error(
            "Trace capture is off: trace %s already exists; pick a new VIGIL_MEDIC_TRACE",
            trace_id,
        )
        return capture
    except OSError as exc:
        log.error("Trace capture is off: %s", type(exc).__name__)
        return capture
    capture._begin(trace_dir, medic, tick)
    log.info("Trace capture on: %s", trace_dir)
    return capture


def recover(traces: Path, medic: dict[str, Any]) -> None:
    """Finalise every trace directory a killed Medic left without a manifest."""
    if not traces.is_dir():
        return
    for trace_dir in sorted(traces.iterdir()):
        if (
            trace_dir.is_symlink()
            or not trace_dir.is_dir()
            or (trace_dir / "manifest.json").exists()
        ):
            continue
        try:
            _finalise(trace_dir, medic)
        except Exception as exc:  # noqa: BLE001 (one bad dir, not the rest)
            log.error("Trace %s not finalised: %s", trace_dir.name, type(exc).__name__)


def _finalise(trace_dir: Path, medic: dict[str, Any]) -> None:
    arrived: list[datetime] = []
    obs_dir = trace_dir / OBSERVATIONS
    for path in sorted(obs_dir.glob("*.jsonl")) if obs_dir.is_dir() else ():
        keep = _trim(path)
        if not keep:
            path.unlink()
            continue
        lines = keep.split(b"\n")
        for raw in (lines[0], lines[-2]):
            arrived.append(datetime.fromisoformat(json.loads(raw)["arrived"]))
    if not arrived:  # nothing was written
        for leftover in (obs_dir, trace_dir):
            leftover.rmdir()
        log.info("Trace %s had no observations: removed", trace_dir.name)
        return
    started, ended = min(arrived), max(arrived)
    if ended <= started:
        ended = datetime.fromtimestamp(started.timestamp() + 1, started.tzinfo)
    sources = [_source(p) for p in sorted(obs_dir.glob("*.jsonl"))]
    _write_manifest(
        trace_dir,
        medic,
        sources,
        iso(started.timestamp()),
        iso(ended.timestamp()),
        True,
    )
    problems = validate(trace_dir)
    log.warning(
        "Trace %s was interrupted: finalised%s",
        trace_dir.name,
        (
            f", but doesn't validate: {sorted({v.code for v in problems})}"
            if problems
            else ""
        ),
    )
