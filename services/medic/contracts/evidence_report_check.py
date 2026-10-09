"""Reference checker for the Medic evidence report (X3, medic.evidence/v1).

One function, two callers:
- H5's generator runs it on its own output before the console shows the preview, and shows
  nothing if it fails (fail closed, K1 T-31).
- DeepTempo runs it on the file the admin sent, before anything reads the counts. A partner's
  security team can run it too: `python evidence_report_check.py report.json`.

`check(data)` takes the exact bytes and returns a sorted list of error codes (empty = valid):

  R-SIZE       over 256 KiB
  R-ENCODING   not pure ASCII
  R-JSON       not JSON, or a duplicate key
  R-CANONICAL  bytes differ from canonical(parsed): extra whitespace, key order, escapes
  R-SCHEMA     fails evidence-report.schema.json
  R-PERIOD     seconds != to - from, or generated_at before to
  R-STATES     availability states don't add up to the period, or up_s != running + degraded
  R-ROW        an incident row's counts contradict each other
  R-ORDER      a list isn't in its canonical order, or has a duplicate key
  R-EXPOSURE   exposure longer than load or up-time, or an incident rule has no exposure row
  R-CHAIN      period seq range outside the head, or half null
  R-SIGNAL     a sensors_blind signal isn't in the D1 catalogue (signal_ids.json)
  R-RULE       a rule (id, revision) isn't in the pack catalogue passed as known_rules
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).parent
SCHEMA = json.loads((ROOT / "evidence-report.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)
SIGNALS = frozenset(json.loads((ROOT / "signal_ids.json").read_text())["signals"])
MAX_BYTES = 256 * 1024
UP_STATES = ("running", "degraded")


def canonical(report: dict) -> bytes:
    """The only spelling of a report: sorted keys, 2-space indent, ASCII, LF, trailing newline."""
    text = json.dumps(
        report, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False
    )
    return (text + "\n").encode("ascii")


def sha256(data: bytes) -> str:
    """What the console shows next to the preview and X-Medic-Sha256 carries (X2)."""
    return hashlib.sha256(data).hexdigest()


def _no_duplicates(pairs: list) -> dict:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _no_constants(name: str):
    raise ValueError(f"{name} is not JSON")


def _ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _sorted_unique(rows: list, key) -> bool:
    keys = [key(r) for r in rows]
    return keys == sorted(keys) and len(keys) == len(set(keys))


def _invariants(r: dict, known_rules: frozenset | None) -> set[str]:
    errors: set[str] = set()
    period = r["period"]
    span = int((_ts(period["to"]) - _ts(period["from"])).total_seconds())
    if span != period["seconds"] or _ts(r["generated_at"]) < _ts(period["to"]):
        errors.add("R-PERIOD")

    avail = r["availability"]
    states = avail["states"]
    if sum(s["seconds"] for s in states.values()) != period["seconds"]:
        errors.add("R-STATES")
    if avail["up_s"] != sum(states[s]["seconds"] for s in UP_STATES):
        errors.add("R-STATES")

    for row in r["incidents"]:
        fb, adj = row["feedback_live"], row["adjudication"]
        wl = row["wrong_lane_suggested"]
        if (
            row["routed"] + row["suppressed_quiet"] > row["opened"]
            or row["open_at_end"] > row["opened"]
            or fb["unrated"] > row["routed"]
            or sum(adj.values()) != row["routed"]
            or sum(wl.values()) > fb["wrong_lane"]
            or (row["lane_reason"] == "lane1_gate_failed" and row["lane"] != 3)
        ):
            errors.add("R-ROW")
    u = r["unknown_signature"]
    if u["routed"] > u["opened"]:
        errors.add("R-ROW")

    lists = [
        (r["packs"], lambda p: (p["id"], p["version"])),
        (r["sensors_blind"], lambda s: s["signal"]),
        (r["rule_exposure"], lambda e: (e["rule_id"], e["revision"])),
        (
            r["incidents"],
            lambda i: (i["rule_id"], i["revision"], i["lane"], i["lane_reason"]),
        ),
        (r["watcher_incidents"], lambda w: w["rule_id"]),
    ]
    if not all(_sorted_unique(rows, key) for rows, key in lists):
        errors.add("R-ORDER")
    if r["replay_feedback"]["heads"] != sorted(r["replay_feedback"]["heads"]):
        errors.add("R-ORDER")
    if r["availability"]["store_states_seen"] != sorted(
        r["availability"]["store_states_seen"]
    ):
        errors.add("R-ORDER")

    exposed = {(e["rule_id"], e["revision"]) for e in r["rule_exposure"]}
    for e in r["rule_exposure"]:
        if (
            e["exposed_s"] > e["loaded_s"]
            or e["loaded_s"] > period["seconds"]
            or e["exposed_s"] > avail["up_s"]
        ):
            errors.add("R-EXPOSURE")
    if any((i["rule_id"], i["revision"]) not in exposed for i in r["incidents"]):
        errors.add("R-EXPOSURE")
    if any(s["seconds"] > avail["up_s"] for s in r["sensors_blind"]):
        errors.add("R-EXPOSURE")

    chain = r["chain"]
    first, last = chain["first_seq_in_period"], chain["last_seq_in_period"]
    if (first is None) != (last is None) or (
        first is not None
        and not (chain["oldest"]["seq"] <= first <= last <= chain["head"]["seq"])
    ):
        errors.add("R-CHAIN")

    if any(s["signal"] not in SIGNALS for s in r["sensors_blind"]):
        errors.add("R-SIGNAL")
    if known_rules is not None:
        used = exposed | {(i["rule_id"], i["revision"]) for i in r["incidents"]}
        if not used <= known_rules:
            errors.add("R-RULE")
    return errors


def check(data: bytes, known_rules: frozenset | None = None) -> list[str]:
    """Validate the exact bytes of a report. known_rules: {(rule_id, revision)} from the packs."""
    if len(data) > MAX_BYTES:
        return ["R-SIZE"]
    if not data.isascii():
        return ["R-ENCODING"]
    try:
        report = json.loads(
            data, object_pairs_hook=_no_duplicates, parse_constant=_no_constants
        )
    except ValueError:
        return ["R-JSON"]
    errors: set[str] = set()
    if not isinstance(report, dict) or canonical(report) != data:
        errors.add("R-CANONICAL")
    if not VALIDATOR.is_valid(report):
        errors.add("R-SCHEMA")
        return sorted(errors)
    errors |= _invariants(report, known_rules)
    return sorted(errors)


if __name__ == "__main__":
    raw = Path(sys.argv[1]).read_bytes()
    found = check(raw)
    print(sha256(raw), "OK" if not found else " ".join(found))
    sys.exit(1 if found else 0)
