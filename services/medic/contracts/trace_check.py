"""Reference checker for a Medic trace directory (D7, trace format v1).

  <trace-id>/
    manifest.json                  trace-manifest.schema.json
    labels.json                    optional, trace-labels.schema.json
    observations/<sensor>.jsonl    one $defs/line per line
    NOTES.md                       optional

`validate(trace_dir)` returns every violation it finds (empty = valid). Capture (#2018)
runs it on what it wrote; replay (#2019) runs it first and refuses the trace if anything
comes back. An interrupted trace is valid: callers read capture.interrupted themselves.

  T-MANIFEST        manifest.json missing, unreadable, not JSON or a duplicate key
  T-MANIFEST-SCHEMA fails trace-manifest.schema.json
  T-LABELS          labels.json unreadable (a broken link too), not JSON or a duplicate key
  T-LABELS-SCHEMA   fails trace-labels.schema.json
  Files
  T-TRACE-ID        trace_id isn't the directory name
  T-FILE-PATH       a source's file isn't observations/<its sensor>.jsonl
  T-SENSOR-DUP      a sensor is listed twice
  T-FILE-MISSING    a listed file doesn't exist or can't be read
  T-FILE-HASH       a listed file's sha256 doesn't match
  T-FILE-LINES      a listed file's line count doesn't match
  T-FILE-UNLISTED   a file that is neither listed, labels.json nor NOTES.md
  Lines
  T-LINE-JSON       a line isn't UTF-8 JSON, or has a duplicate key
  T-LINE-SCHEMA     a line fails trace-manifest.schema.json $defs/line
  T-SEQ             seq doesn't strictly increase within a file
  T-ARRIVED-ORDER   arrived goes backwards within a file
  T-ARRIVED-SPAN    arrived lies outside capture
  T-OBSERVATION     an observation fails observation.schema.json
  T-LINE-SENSOR     an observation's sensor.id isn't the file's sensor
  Capture
  T-CAPTURE         ended_at isn't after started_at
  Labels (only when labels.json exists)
  T-LABEL-ID        a fault trace's id doesn't begin with its cause, a control's with "control"
  T-WINDOWS         windows out of order, not contiguous, empty, or an expectation for a missing window
  T-WINDOW-SPAN     windows reach outside capture
  T-CONTROL         a control trace has more than a baseline, an injection or something in fires
  T-FAULT-SHAPE     a fault trace lacks its fault/recovery window, injection, recovery_action or fault expectation
  T-FIRES-MODE      a fault trace's expected.fault.fires doesn't include its mode
  T-FIRES-OVERLAP   a mode is in both fires and may_fire
  T-DETECT-WITHIN   detect_within_s is longer than its window
  T-CLEAR-WITHIN    clear_within_s is longer than its window
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import NamedTuple

from jsonschema import Draft202012Validator, FormatChecker

HERE = Path(__file__).parent
# The ts pattern admits "2026-02-30"; format: date-time refuses what won't parse below.
_FORMATS = FormatChecker(formats=())
_FORMATS.checks("date-time", raises=ValueError)(datetime.fromisoformat)
_MANIFEST = json.loads((HERE / "trace-manifest.schema.json").read_text())
MANIFEST = Draft202012Validator(_MANIFEST, format_checker=_FORMATS)
LINE = Draft202012Validator(
    {
        "$schema": _MANIFEST["$schema"],
        "$defs": _MANIFEST["$defs"],
        "$ref": "#/$defs/line",
    },
    format_checker=_FORMATS,
)
LABELS = Draft202012Validator(
    json.loads((HERE / "trace-labels.schema.json").read_text()),
    format_checker=_FORMATS,
)
OBSERVATION = Draft202012Validator(
    json.loads((HERE / "observation.schema.json").read_text())
)
OPTIONAL_FILES = {"labels.json", "NOTES.md"}
WINDOWS = ("baseline", "fault", "recovery")


class Violation(NamedTuple):
    code: str
    detail: str


def _no_dupes(pairs: list) -> dict:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError(
            f"duplicate JSON keys {sorted({k for k in keys if keys.count(k) > 1})}"
        )
    return dict(pairs)


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-standard JSON constant {name}")


def _json(text: str):
    return json.loads(
        text, object_pairs_hook=_no_dupes, parse_constant=_reject_constant
    )


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _lines(data: bytes) -> list[bytes]:
    """Newline-terminated lines; a last line without its newline still counts."""
    parts = data.split(b"\n")
    return parts[:-1] if parts[-1] == b"" else parts


def _schema(
    validator: Draft202012Validator, doc, code: str, where: str = ""
) -> list[Violation]:
    return [
        Violation(code, f"{where}{'/'.join(map(str, e.path))}: {e.message[:150]}")
        for e in sorted(validator.iter_errors(doc), key=lambda e: list(e.path))[:10]
    ]


def validate(trace_dir: str | Path) -> list[Violation]:
    root = Path(trace_dir)
    out: list[Violation] = []
    try:
        manifest = _json((root / "manifest.json").read_text("utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        return [Violation("T-MANIFEST", str(exc)[:200])]
    out += _schema(MANIFEST, manifest, "T-MANIFEST-SCHEMA")
    if out:  # nothing below is meaningful against a malformed manifest
        return out
    labels = None
    if os.path.lexists(root / "labels.json"):  # a broken link is unreadable, not absent
        try:
            labels = _json((root / "labels.json").read_text("utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            out.append(Violation("T-LABELS", str(exc)[:200]))
        else:
            schema_errors = _schema(LABELS, labels, "T-LABELS-SCHEMA")
            out += schema_errors
            labels = None if schema_errors else labels

    cap = manifest["capture"]
    start, end = _ts(cap["started_at"]), _ts(cap["ended_at"])

    # Files: the manifest accounts for every file, and every byte of each.
    if manifest["trace_id"] != root.resolve().name:
        out.append(
            Violation(
                "T-TRACE-ID",
                f"trace_id {manifest['trace_id']} != directory {root.resolve().name}",
            )
        )
    seen: set[str] = set()
    for src in manifest["sources"]:
        if src["file"] != f"observations/{src['sensor']}.jsonl":
            out.append(
                Violation(
                    "T-FILE-PATH",
                    f"{src['file']}: must be observations/{src['sensor']}.jsonl",
                )
            )
        if src["sensor"] in seen:
            out.append(
                Violation("T-SENSOR-DUP", f"sensor {src['sensor']} is listed twice")
            )
        seen.add(src["sensor"])
    listed = {src["file"] for src in manifest["sources"]}
    # A symlink is never a directory here, so a link out of the trace is reported.
    present = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_symlink() or not p.is_dir()
    }
    for path in sorted(present - listed - OPTIONAL_FILES - {"manifest.json"}):
        out.append(Violation("T-FILE-UNLISTED", f"{path}: not in the manifest"))
    for src in manifest["sources"]:
        path = root / src["file"]
        try:
            data = path.read_bytes()
        except OSError as exc:
            out.append(Violation("T-FILE-MISSING", f"{src['file']}: {exc.strerror}"))
            continue
        if hashlib.sha256(data).hexdigest() != src["sha256"]:
            out.append(
                Violation(
                    "T-FILE-HASH", f"{src['file']}: sha256 doesn't match the manifest"
                )
            )
        lines = _lines(data)
        if len(lines) != src["lines"]:
            out.append(
                Violation(
                    "T-FILE-LINES",
                    f"{src['file']}: {len(lines)} lines, manifest says {src['lines']}",
                )
            )
        out += _check_lines(src["sensor"], src["file"], lines, start, end)

    # Capture
    if not start < end:
        out.append(
            Violation(
                "T-CAPTURE",
                f"ended_at {cap['ended_at']} is not after started_at {cap['started_at']}",
            )
        )

    if labels is not None:
        out += _check_labels(labels, manifest["trace_id"], start, end)
    return out


def _check_lines(
    sensor: str, name: str, lines: list[bytes], start: datetime, end: datetime
) -> list[Violation]:
    out: list[Violation] = []
    prev_seq: int | None = None
    prev_arrived: datetime | None = None
    for n, raw in enumerate(lines, 1):
        where = f"{name}:{n}: "
        try:
            line = _json(raw.decode("utf-8"))
        except (
            ValueError,
            RecursionError,
        ) as exc:  # UnicodeDecodeError is a ValueError
            out.append(Violation("T-LINE-JSON", where + str(exc)[:150]))
            continue
        errs = _schema(LINE, line, "T-LINE-SCHEMA", where)
        if errs:
            out += errs
            continue
        if prev_seq is not None and line["seq"] <= prev_seq:
            out.append(Violation("T-SEQ", f"{where}seq {line['seq']} after {prev_seq}"))
        arrived = _ts(line["arrived"])
        if prev_arrived is not None and arrived < prev_arrived:
            out.append(
                Violation(
                    "T-ARRIVED-ORDER",
                    f"{where}arrived {line['arrived']} goes backwards",
                )
            )
        if not start <= arrived <= end:
            out.append(
                Violation(
                    "T-ARRIVED-SPAN",
                    f"{where}arrived {line['arrived']} is outside capture",
                )
            )
        prev_seq, prev_arrived = line["seq"], arrived
        out += _schema(
            OBSERVATION, line["observation"], "T-OBSERVATION", f"{where}observation/"
        )
        got = line["observation"].get("sensor")
        got = got.get("id") if isinstance(got, dict) else None
        if isinstance(got, str) and got != sensor:
            out.append(
                Violation(
                    "T-LINE-SENSOR", f"{where}observation from {got}, not {sensor}"
                )
            )
    return out


def _check_labels(
    labels: dict, trace_id: str, start: datetime, end: datetime
) -> list[Violation]:
    out: list[Violation] = []
    fault, windows, expected = labels["fault"], labels["windows"], labels["expected"]
    control = fault["mode"] is None

    prefix = "control" if control else fault["cause"]
    if not (trace_id == prefix or trace_id.startswith(prefix + "-")):
        out.append(
            Violation("T-LABEL-ID", f"trace_id {trace_id} must begin with {prefix}-")
        )

    # Windows: baseline → fault → recovery, each non-empty, each ending where the next starts.
    names = [w for w in WINDOWS if w in windows]
    if names != list(WINDOWS[: len(names)]):
        out.append(Violation("T-WINDOWS", f"windows {names} skip one"))
    span = {w: (_ts(windows[w]["start"]), _ts(windows[w]["end"])) for w in names}
    for w in names:
        if not span[w][0] < span[w][1]:
            out.append(Violation("T-WINDOWS", f"{w}: end is not after start"))
    for a, b in pairwise(names):
        if span[a][1] != span[b][0]:
            out.append(
                Violation(
                    "T-WINDOWS",
                    f"{a} ends at {windows[a]['end']} but {b} starts at {windows[b]['start']}",
                )
            )
    for w in sorted(set(expected) - set(windows)):
        out.append(Violation("T-WINDOWS", f"expected.{w} has no window"))
    if span[names[0]][0] < start or span[names[-1]][1] > end:
        out.append(Violation("T-WINDOW-SPAN", "windows reach outside capture"))

    if control:
        if names != ["baseline"]:
            out.append(
                Violation(
                    "T-CONTROL",
                    f"a control trace has only a baseline window, not {names}",
                )
            )
        for key in ("injection", "recovery_action"):
            if key in labels:
                out.append(Violation("T-CONTROL", f"a control trace has no {key}"))
        for w, exp in expected.items():
            if exp["fires"]:
                out.append(
                    Violation(
                        "T-CONTROL",
                        f"expected.{w}.fires must be empty in a control trace",
                    )
                )
    else:
        missing = [w for w in ("fault", "recovery") if w not in windows]
        missing += [k for k in ("injection", "recovery_action") if k not in labels]
        missing += ["expected.fault"] if "fault" not in expected else []
        if missing:
            out.append(Violation("T-FAULT-SHAPE", f"a fault trace needs {missing}"))
        if "fault" in expected and fault["mode"] not in expected["fault"]["fires"]:
            out.append(
                Violation(
                    "T-FIRES-MODE", f"expected.fault.fires must include {fault['mode']}"
                )
            )

    for w, exp in expected.items():
        if both := sorted(set(exp["fires"]) & set(exp["may_fire"])):
            out.append(
                Violation(
                    "T-FIRES-OVERLAP",
                    f"expected.{w}: {both} in both fires and may_fire",
                )
            )
        if w not in span or span[w][0] >= span[w][1]:
            continue  # a missing or inverted window is already T-WINDOWS
        length = (span[w][1] - span[w][0]).total_seconds()
        for key, code in (
            ("detect_within_s", "T-DETECT-WITHIN"),
            ("clear_within_s", "T-CLEAR-WITHIN"),
        ):
            if exp[key] is not None and exp[key] > length:
                out.append(
                    Violation(
                        code,
                        f"expected.{w}.{key} {exp[key]} > the {w} window ({length:.0f}s)",
                    )
                )
    return out
