"""Expand a compact evaluation vector (vector.schema.json) into D3 observations.

The engine (E4) consumes the same expansion. Sensors, series and logs become
sample, log and sensor_health observations, ordered by observed_at. Rules:
- A series emits one sample per covering-sensor interval while that sensor is 'ok'.
- While a sensor is 'error', its signals emit outcome=error samples. After 3 failed
  intervals, its heartbeat says 'blind'.
- While a sensor is 'stopped', nothing is emitted by it. After 3 silent intervals the
  framework emits 'stopped' heartbeats on its behalf (D3 decision 2a).
- Log lines are emitted only while their log sensor is 'ok'.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

if __package__:  # inside the services.medic package (A3 F-16)
    from .fingerprint_ref import LogLine, fingerprint
else:  # run from the contracts folder (tests, CLI)
    from fingerprint_ref import LogLine, fingerprint

HERE = Path(__file__).parent
RUN = "01VECTORRUN0"


def secs(s: str) -> int:
    """'+29m45s', '15s', '1h' -> seconds."""
    total, num = 0, ""
    for ch in s.lstrip("+"):
        if ch.isdigit():
            num += ch
        else:
            total += int(num) * {"s": 1, "m": 60, "h": 3600}[ch]
            num = ""
    return total


def iso(start: datetime, offset_s: float) -> str:
    return (start + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _state_at(states: list, t: int) -> str:
    current = "stopped"
    for off, st in states:
        if secs(off) <= t:
            current = st
    return current


def _step_at(steps: list, t: int):
    value, seen = None, False
    for off, v in steps:
        if secs(off) <= t:
            value, seen = v, True
    return value if seen else "__none__"


def expand(vector: dict) -> list[dict]:
    start = datetime.strptime(vector["start"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    end = secs(vector["end"])
    types = vector.get("types", {})
    sensors = vector["sensors"]
    by_signal = {sig: s for s in sensors for sig in s["covers"]}
    out: list[dict] = []
    seq: dict[str, int] = {}

    def base(kind: str, signal: str, sensor: dict, t: int) -> dict:
        n = seq[sensor["id"]] = seq.get(sensor["id"], -1) + 1
        return {
            "v": 1,
            "id": f"{sensor['id']}:{RUN}:{n}",
            "kind": kind,
            "signal": signal,
            "sensor": {"id": sensor["id"], "run": RUN, "seq": n},
            "target": {
                "service": sensor["service"],
                "shape": sensor.get("shape", "compose"),
            },
            "t": iso(start, t),
            "observed_at": iso(start, t),
            "source_ts": None,
            "outcome": "ok",
        }

    # Samples, grouped per (signal, time) into one observation.
    samples: dict[tuple[str, int], list[dict]] = {}
    epochs: dict[tuple[str, int], str] = {}
    for s in vector.get("series", []):
        sensor = by_signal[s["signal"]]
        step = secs(sensor["interval"])
        lo, hi = secs(s.get("from", "+0s")), secs(s.get("to", vector["end"]))
        for t in range(lo, min(hi, end) + 1, step):
            value = _step_at(s["steps"], t)
            if value == "__none__":
                continue
            vtype = types.get(s["signal"], {}).get(s["key"], "gauge")
            entry = {
                "key": s["key"],
                "type": vtype,
                "state": "absent" if value is None else "present",
                "value": value,
                "trust": "untrusted" if vtype == "text" else "trusted",
            }
            if s.get("labels"):
                entry["labels"] = s["labels"]
            samples.setdefault((s["signal"], t), []).append(entry)
            if s.get("epochs"):
                epochs[(s["signal"], t)] = _step_at(s["epochs"], t)
    for (signal, t), values in sorted(
        samples.items(), key=lambda kv: (kv[0][1], kv[0][0])
    ):
        sensor = by_signal[signal]
        state = _state_at(sensor["states"], t)
        if state == "stopped":
            continue
        obs = base("sample", signal, sensor, t)
        obs["redaction"] = {"version": "k2-0", "hits": 0}
        if state == "error":
            obs["outcome"] = "error"
            obs["values"] = []
            obs["error"] = {"class": "refused"}
        else:
            obs["values"] = values
            if any(v["type"] == "counter" for v in values):
                obs["epoch"] = epochs.get((signal, t), "e1")
        out.append(obs)

    # Logs.
    for line in vector.get("logs", []):
        sensor = by_signal[line["signal"]]
        times = [secs(line["at"])]
        if "repeat" in line:
            every, until = secs(line["repeat"]["every"]), secs(line["repeat"]["until"])
            times = list(range(times[0], until + 1, every))
        for t in times:
            if _state_at(sensor["states"], t) != "ok" or t > end:
                continue
            message = line.get("message", line["template"])
            trusted = line.get("trusted", True)
            catalog = {(line["logger"], line["template"])} if trusted else set()
            fp = fingerprint(
                LogLine(
                    sensor["service"],
                    "python_json",
                    line["level"],
                    line["logger"],
                    line["template"],
                    message,
                ),
                catalog,
            )
            obs = base("log", line["signal"], sensor, t)
            obs["source_ts"] = iso(start, t)
            obs["skew_ms"] = 0
            obs["redaction"] = {"version": "k2-0", "hits": 0}
            ts = obs["source_ts"]
            lh = hashlib.sha256(
                f"{sensor['service']}||{ts}|{message}".encode()
            ).hexdigest()[:16]
            obs["log"] = {
                "format": "python_json",
                "level": line["level"],
                "logger": line["logger"],
                "template": line["template"],
                "template_trust": fp.template_trust,
                "message": message[:500],
                "fingerprint": fp.fingerprint,
                "fingerprint_basis": fp.basis,
                "line_hash": lh,
            }
            if fp.args:
                obs["log"]["args"] = list(fp.args)
            out.append(obs)

    # Heartbeats.
    for sensor in sensors:
        step = secs(sensor["interval"])
        last_ok, fails, silent = None, 0, 0
        for t in range(0, end + 1, step):
            state = _state_at(sensor["states"], t)
            if state == "stopped":
                silent += 1
                if silent < 3:
                    continue
                by = "framework"
                hstate, consecutive = "stopped", 0
            else:
                silent = 0
                fails = fails + 1 if state == "error" else 0
                last_ok = t if state == "ok" else last_ok
                by = "sensor"
                hstate = (
                    "ok" if state == "ok" else ("blind" if fails >= 3 else "degraded")
                )
                consecutive = fails
            hb = base(
                "sensor_health",
                "sensor_health",
                sensor if by == "sensor" else {**sensor, "id": "framework"},
                t,
            )
            hb["health"] = {
                "sensor": sensor["id"],
                "state": hstate,
                "reported_by": by,
                "covers": sensor["covers"],
                "interval_s": step,
                "last_ok_at": iso(start, last_ok) if last_ok is not None else None,
                "consecutive_failures": consecutive,
                "counters": {
                    "reads": 0,
                    "failures": fails,
                    "dropped": 0,
                    "redactor_failures": 0,
                },
            }
            if hstate != "ok":
                hb["health"]["last_error"] = {"class": "refused"}
            if hstate == "ok":
                hb["health"]["counters"]["failures"] = 0
            out.append(hb)

    return sorted(out, key=lambda o: (o["observed_at"], o["kind"], o["id"]))


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())
