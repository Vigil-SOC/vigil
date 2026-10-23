"""Regenerate D7 trace fixtures (hashes and line counts are computed, never hand-typed).

uv run python tools/gen_d7_traces.py

valid/<trace-id>/              trace directories trace_check.validate accepts
invalid/<case>/<trace-id>/     one defect each; invalid/expected.json names its codes
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
OUT = ROOT / "fixtures" / "traces"
OBS = ROOT / "fixtures" / "observations" / "valid"
T0 = datetime(2026, 10, 7, 10, tzinfo=UTC)
RUN = "01JA8Z3K4M"


def ts(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def observation(template: str, sensor: str, seq: int, at: str, **over) -> dict:
    o = json.loads((OBS / template).read_text())
    o.update(id=f"{sensor}:{RUN}:{seq}", t=at, observed_at=at, **over)
    o["sensor"] = {"id": sensor, "run": RUN, "seq": seq}
    if o["kind"] == "sensor_health":
        o["health"].update(sensor=sensor, last_ok_at=at)
    return o


def trace(
    trace_id: str,
    *,
    minutes: int = 30,
    fault: tuple[int, int] | None = None,
    interrupted=False,
) -> dict:
    """Two sensors: a triage sample every 2 min (503 inside `fault`), a heartbeat every 5."""
    events = []
    for m in range(1, minutes, 2):
        failing = fault and fault[0] <= m < fault[1]
        tpl = (
            "endpoint-error-triage-503.json"
            if failing
            else "endpoint-json-federation-sources.json"
        )
        over = {
            "signal": "triage",
            "target": {
                "service": "backend",
                "instance": "deeptempo-backend",
                "shape": "compose",
            },
        }
        events.append((m, "http.triage", tpl, over))
    for m in range(0, minutes, 5):
        events.append((m + 0.5, "http.daemon_status", "sensor-health-ok.json", {}))
    obs: dict[str, list[dict]] = {}
    for seq, (m, sensor, tpl, over) in enumerate(sorted(events)):
        lines = obs.setdefault(sensor, [])
        lines.append(
            {
                "seq": seq,
                "arrived": ts(m),
                "observation": observation(tpl, sensor, len(lines), ts(m), **over),
            }
        )
    return {
        "manifest": {
            "schema_version": "1",
            "trace_id": trace_id,
            "provenance": "synthetic",
            "medic": {
                "version": "0.1.0",
                "install_shape": "compose",
                "instance_id": "mi_3f9a2c1b0d4e5f60",
                "pack": {"id": "medic-core", "version": "0.1.0"},
            },
            "capture": {
                "started_at": ts(0),
                "ended_at": ts(minutes),
                "interrupted": interrupted,
            },
            "sources": [],
        },
        "labels": None,
        "obs": obs,
        "extra": {},
    }


def no_fire() -> dict:
    return {
        "fires": [],
        "may_fire": [],
        "detect_within_s": None,
        "clear_within_s": None,
    }


def fault_trace() -> dict:
    t = trace("P-4.a-triage-503", fault=(10, 20))
    t["labels"] = {
        "fault": {
            "mode": "P-4",
            "cause": "P-4.a",
            "class": "pipeline",
            "lane_expected": 3,
        },
        "windows": {
            "baseline": {"start": ts(0), "end": ts(10)},
            "fault": {"start": ts(10), "end": ts(20)},
            "recovery": {"start": ts(20), "end": ts(30)},
        },
        "injection": {"at": ts(10), "actions": ["docker compose stop backend-db"]},
        "recovery_action": {
            "at": ts(20),
            "actions": ["docker compose start backend-db"],
        },
        "expected": {
            "baseline": no_fire(),
            "fault": {
                "fires": ["P-4"],
                "may_fire": ["L-4"],
                "detect_within_s": 300,
                "clear_within_s": None,
            },
            "recovery": {
                "fires": [],
                "may_fire": ["P-4"],
                "detect_within_s": None,
                "clear_within_s": 300,
            },
        },
    }
    t["extra"]["NOTES.md"] = (
        b"Synthetic: triage answers 503 while its database is stopped.\n"
    )
    return t


def control_trace() -> dict:
    t = trace("control-quiet-compose")
    t["labels"] = {
        "fault": {"mode": None, "cause": None, "class": None, "lane_expected": None},
        "windows": {"baseline": {"start": ts(0), "end": ts(30)}},
        "expected": {
            "baseline": {
                "fires": [],
                "may_fire": ["L-4"],
                "detect_within_s": None,
                "clear_within_s": None,
            }
        },
    }
    return t


def render(t: dict) -> dict[str, bytes]:
    files = {
        f"observations/{s}.jsonl": b"".join(
            json.dumps(x).encode() + b"\n" for x in lines
        )
        for s, lines in t["obs"].items()
    }
    t["manifest"]["sources"] = [
        {
            "sensor": s,
            "file": p,
            "sha256": hashlib.sha256(files[p]).hexdigest(),
            "lines": len(t["obs"][s]),
        }
        for s, p in ((s, f"observations/{s}.jsonl") for s in sorted(t["obs"]))
    ]
    files["manifest.json"] = (json.dumps(t["manifest"], indent=2) + "\n").encode()
    if t["labels"] is not None:
        files["labels.json"] = (json.dumps(t["labels"], indent=2) + "\n").encode()
    return files | t["extra"]


def write(where: Path, t: dict, after: Callable[[dict], None] | None = None) -> None:
    files = render(t)
    if after:  # breaks what render() just made truthful
        after(files)
    d = where / t["manifest"]["trace_id"]
    for path, data in files.items():
        (d / path).parent.mkdir(parents=True, exist_ok=True)
        (d / path).write_bytes(data)


def manifest_edit(fn: Callable[[dict], None]) -> Callable[[dict], None]:
    def after(files: dict) -> None:
        m = json.loads(files["manifest.json"])
        fn(m)
        files["manifest.json"] = (json.dumps(m, indent=2) + "\n").encode()

    return after


def line_edit(sensor: str, fn: Callable[[list[dict]], None]) -> Callable[[dict], None]:
    """Edit an observations file before render() hashes it."""

    def edit(t: dict) -> None:
        fn(t["obs"][sensor])

    return edit


def set_labels(
    fn: Callable[[dict], None], base: Callable[[], dict] = fault_trace
) -> dict:
    t = base()
    fn(t["labels"])
    return t


TRIAGE = "http.triage"
CASES: dict[str, tuple[str, str, dict, Callable[[dict], None] | None]] = {}


def case(name: str, code: str, why: str, t: dict, after=None) -> None:
    CASES[name] = (code, why, t, after)


def edited(t: dict, fn: Callable[[dict], None]) -> dict:
    fn(t)
    return t


def build_cases() -> None:
    def bad_json(files):
        files["manifest.json"] = b"{not json"

    case(
        "manifest-not-json",
        "T-MANIFEST",
        "manifest.json isn't JSON",
        trace("capture-plain"),
        bad_json,
    )
    case(
        "manifest-extra-field",
        "T-MANIFEST-SCHEMA",
        "the manifest is closed",
        trace("capture-plain"),
        manifest_edit(lambda m: m.update(note="x")),
    )
    case(
        "labels-dup-key",
        "T-LABELS",
        "labels.json has a duplicate key",
        fault_trace(),
        lambda f: f.update({"labels.json": b'{"fault": {}, "fault": {}}'}),
    )
    case(
        "labels-mode-pattern",
        "T-LABELS-SCHEMA",
        "fault.mode must look like a rule's fault.mode",
        set_labels(lambda lb: lb["fault"].update(mode="P-4.a")),
    )
    case(
        "labels-half-null-fault",
        "T-LABELS-SCHEMA",
        "fault fields are all set or all null",
        set_labels(lambda lb: lb["fault"].update(lane_expected=None)),
    )
    # Files
    case(
        "trace-id-not-dir",
        "T-TRACE-ID",
        "trace_id differs from the directory",
        trace("capture-plain"),
        manifest_edit(lambda m: m.update(trace_id="capture-other")),
    )

    def misnamed(files):
        old, new = f"observations/{TRIAGE}.jsonl", "observations/triage.jsonl"
        files[new] = files.pop(old)
        manifest_edit(
            lambda m: [s.update(file=new) for s in m["sources"] if s["file"] == old]
        )(files)

    case(
        "file-path-mismatch",
        "T-FILE-PATH",
        "a source's file must be named after its sensor",
        trace("capture-plain"),
        misnamed,
    )

    def dup_sensor(files):
        manifest_edit(lambda m: m["sources"].append(dict(m["sources"][0])))(files)

    case(
        "sensor-listed-twice",
        "T-SENSOR-DUP",
        "each sensor is listed once",
        trace("capture-plain"),
        dup_sensor,
    )

    def missing(files):
        del files[f"observations/{TRIAGE}.jsonl"]

    case(
        "file-missing",
        "T-FILE-MISSING",
        "a listed file doesn't exist",
        trace("capture-plain"),
        missing,
    )

    def flip_byte(files):
        p = f"observations/{TRIAGE}.jsonl"
        files[p] = files[p].replace(b'"hits": 0', b'"hits": 1', 1)

    case(
        "file-hash",
        "T-FILE-HASH",
        "a file changed after capture",
        trace("capture-plain"),
        flip_byte,
    )
    case(
        "file-lines",
        "T-FILE-LINES",
        "the manifest's line count is wrong",
        trace("capture-plain"),
        manifest_edit(
            lambda m: m["sources"][0].update(lines=m["sources"][0]["lines"] + 1)
        ),
    )
    case(
        "file-unlisted",
        "T-FILE-UNLISTED",
        "a file the manifest doesn't list",
        trace("capture-plain"),
        lambda f: f.update({"observations/stray.jsonl": b""}),
    )

    # Lines (edited before hashing, so only the line defect remains)
    def bad_line(files):
        p = "observations/http.daemon_status.jsonl"
        files[p] += b"{oops\n"
        manifest_edit(
            lambda m: m["sources"][0].update(
                sha256=hashlib.sha256(files[p]).hexdigest(),
                lines=m["sources"][0]["lines"] + 1,
            )
        )(files)

    case(
        "line-not-json",
        "T-LINE-JSON",
        "a line doesn't parse",
        trace("capture-plain"),
        bad_line,
    )
    case(
        "line-extra-field",
        "T-LINE-SCHEMA",
        "a line is closed",
        edited(
            trace("capture-plain"), line_edit(TRIAGE, lambda ls: ls[0].update(note="x"))
        ),
    )
    case(
        "seq-repeats",
        "T-SEQ",
        "seq strictly increases within a file",
        edited(
            trace("capture-plain"),
            line_edit(TRIAGE, lambda ls: ls[1].update(seq=ls[0]["seq"])),
        ),
    )
    case(
        "arrived-backwards",
        "T-ARRIVED-ORDER",
        "arrived never goes backwards",
        edited(
            trace("capture-plain"),
            line_edit(TRIAGE, lambda ls: ls[2].update(arrived=ls[0]["arrived"])),
        ),
    )
    case(
        "arrived-after-capture",
        "T-ARRIVED-SPAN",
        "arrived lies within capture",
        edited(
            trace("capture-plain"),
            line_edit(TRIAGE, lambda ls: ls[-1].update(arrived=ts(31))),
        ),
    )

    def other_sensor(lines):
        for x in lines:
            x["observation"]["sensor"]["id"] = "http.other"

    case(
        "line-sensor-mismatch",
        "T-LINE-SENSOR",
        "a file holds only its own sensor's observations",
        edited(trace("capture-plain"), line_edit(TRIAGE, other_sensor)),
    )
    case(
        "arrived-impossible-date",
        "T-LINE-SCHEMA",
        "arrived matches the pattern but isn't a date",
        edited(
            trace("capture-plain"),
            line_edit(TRIAGE, lambda ls: ls[0].update(arrived="2026-10-07T25:00:00Z")),
        ),
    )
    case(
        "observation-invalid",
        "T-OBSERVATION",
        "the observation fails observation.schema.json",
        edited(
            trace("capture-plain"),
            line_edit(TRIAGE, lambda ls: ls[0]["observation"].update(v=2)),
        ),
    )

    # Capture: a trace whose end is its start, with no lines left inside it but the first tick's
    def zero_span(t):
        t["manifest"]["capture"]["ended_at"] = ts(0)
        t["obs"] = {
            "http.daemon_status": [
                {
                    "seq": 0,
                    "arrived": ts(0),
                    "observation": observation(
                        "sensor-health-ok.json", "http.daemon_status", 0, ts(0)
                    ),
                }
            ]
        }

    case(
        "capture-not-after-start",
        "T-CAPTURE",
        "ended_at must be after started_at",
        edited(trace("capture-plain"), zero_span),
    )

    # Labels
    def rename(t, new_id):
        t["manifest"]["trace_id"] = new_id
        return t

    case(
        "fault-id-not-cause",
        "T-LABEL-ID",
        "a fault trace's id begins with its cause",
        rename(fault_trace(), "P-4.b-triage-503"),
    )
    case(
        "control-id",
        "T-LABEL-ID",
        "a control trace's id begins with control",
        rename(control_trace(), "quiet-compose"),
    )
    case(
        "windows-gap",
        "T-WINDOWS",
        "windows are contiguous",
        set_labels(lambda lb: lb["windows"]["fault"].update(start=ts(11))),
    )
    case(
        "window-inverted",
        "T-WINDOWS",
        "each window ends after it starts",
        set_labels(
            lambda lb: lb["windows"].update(
                fault={"start": ts(20), "end": ts(10)},
                recovery={"start": ts(10), "end": ts(30)},
                baseline={"start": ts(0), "end": ts(20)},
            )
        ),
    )
    case(
        "windows-outside-capture",
        "T-WINDOW-SPAN",
        "windows lie inside capture",
        set_labels(lambda lb: lb["windows"]["recovery"].update(end=ts(31))),
    )
    case(
        "control-with-injection",
        "T-CONTROL",
        "a control trace has no injection",
        set_labels(
            lambda lb: lb.update(injection={"at": ts(5), "actions": ["x"]}),
            control_trace,
        ),
    )
    case(
        "control-fires",
        "T-CONTROL",
        "a control trace expects nothing to fire",
        set_labels(
            lambda lb: lb["expected"]["baseline"].update(fires=["P-4"], may_fire=[]),
            control_trace,
        ),
    )

    def control_fault_window(lb):
        lb["windows"] = {
            "baseline": {"start": ts(0), "end": ts(15)},
            "fault": {"start": ts(15), "end": ts(30)},
        }

    case(
        "control-fault-window",
        "T-CONTROL",
        "a control trace has only a baseline",
        set_labels(control_fault_window, control_trace),
    )
    case(
        "fault-without-injection",
        "T-FAULT-SHAPE",
        "a fault trace records its injection",
        set_labels(lambda lb: lb.pop("injection")),
    )
    case(
        "fires-without-mode",
        "T-FIRES-MODE",
        "a fault trace's fault window fires its mode",
        set_labels(
            lambda lb: lb["expected"]["fault"].update(fires=["L-4"], may_fire=[])
        ),
    )
    case(
        "fires-and-may-fire",
        "T-FIRES-OVERLAP",
        "a mode can't be both required and allowed",
        set_labels(lambda lb: lb["expected"]["fault"].update(may_fire=["P-4"])),
    )
    case(
        "detect-longer-than-fault",
        "T-DETECT-WITHIN",
        "detect_within_s fits the fault window",
        set_labels(lambda lb: lb["expected"]["fault"].update(detect_within_s=601)),
    )
    case(
        "clear-longer-than-recovery",
        "T-CLEAR-WITHIN",
        "clear_within_s fits the recovery window",
        set_labels(lambda lb: lb["expected"]["recovery"].update(clear_within_s=601)),
    )


def main() -> None:
    shutil.rmtree(OUT, ignore_errors=True)
    valid = OUT / "valid"
    write(valid, fault_trace())
    write(valid, control_trace())
    write(valid, trace("capture-plain"))
    write(valid, trace("capture-interrupted", minutes=12, interrupted=True))

    build_cases()
    expected = {}
    for name, (code, why, t, after) in CASES.items():
        write(OUT / "invalid" / name, deepcopy(t), after)
        expected[name] = {"codes": [code], "why": why}
    (OUT / "invalid" / "expected.json").write_text(
        json.dumps(expected, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
