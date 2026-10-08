"""Reference rule loader checks for rule schema v1 / engine API 1.0.

The executable form of ENGINE_API.md §5: the pack loader (F6) and the rule test CLI
(E5) must accept and refuse exactly what this accepts and refuses, with the same
error codes. Order: size → YAML (no aliases, no duplicate keys) → JSON Schema →
semantic checks → engine version.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import re2
import yaml
from jsonschema import Draft202012Validator

ENGINE_API = (1, 0)
MAX_RULE_BYTES = 16 * 1024
MAX_DEPTH = 3
MAX_CONDITIONS = 16
MAX_WINDOW_S = 48 * 3600
MAX_FOR_S = 24 * 3600
MAX_RE2_PROGRAM = 1000  # RE2 program size: bounds memory per pattern

HERE = Path(__file__).parent
SCHEMA = Draft202012Validator(json.loads((HERE / "rule.schema.json").read_text()))
_IDS = json.loads((HERE / "signal_ids.json").read_text())
SIGNALS = set(_IDS["signals"])
LOG_SIGNALS = set(_IDS["log_signals"])
SAMPLE_FNS = {"latest", "age", "increase", "rate", "changes"}
LOG_FNS = {"count"}


@dataclass
class Result:
    errors: list[tuple[str, str]] = field(default_factory=list)
    input_trust: str | None = None  # derived: trusted | untrusted
    detection: str = "event"  # declared, default event (X1 ⚑4a)
    skipped: str | None = None  # set when the engine is too old for the rule

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def codes(self) -> set[str]:
        return {code for code, _ in self.errors}


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses aliases (billion-laughs) and duplicate keys
    (a reviewer would see one value while the engine used another)."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("E-YAML-ALIAS: aliases are not allowed")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(k, deep=deep) for k, _ in node.value]
        dupes = {k for k in keys if keys.count(k) > 1}
        if dupes:
            raise yaml.YAMLError(f"E-YAML-DUPKEY: duplicate keys {sorted(dupes)}")
        return super().construct_mapping(node, deep=deep)


def seconds(duration: str) -> int:
    return int(duration[:-1]) * {"s": 1, "m": 60, "h": 3600}[duration[-1]]


def load(text: str) -> tuple[dict | None, Result]:
    res = Result()
    if len(text.encode()) > MAX_RULE_BYTES:
        res.errors.append(("E-SIZE", f"rule larger than {MAX_RULE_BYTES} bytes"))
        return None, res
    try:
        doc = yaml.load(text, Loader=_StrictLoader)
    except yaml.YAMLError as exc:
        msg = str(exc)
        code = (
            "E-YAML-ALIAS"
            if "ALIAS" in msg
            else "E-YAML-DUPKEY"
            if "DUPKEY" in msg
            else "E-YAML"
        )
        res.errors.append((code, msg.splitlines()[0]))
        return None, res
    return doc, res


def check_text(text: str) -> Result:
    doc, res = load(text)
    return res if doc is None else check(doc)


def check(rule: dict) -> Result:
    res = Result()
    for err in sorted(SCHEMA.iter_errors(rule), key=lambda e: list(e.path)):
        res.errors.append(
            ("E-SCHEMA", f"{'/'.join(map(str, err.path))}: {err.message[:120]}")
        )
    if res.errors:
        return res

    signals: dict = rule["signals"]
    params: dict = rule.get("params", {})
    kind = {name: next(iter(sig)) for name, sig in signals.items()}

    # Signals exist in D1 and log signals read logs.
    for name, sig in signals.items():
        sid = sig[kind[name]]["signal"]
        if sid not in SIGNALS:
            res.errors.append(("E-SIGNAL-UNKNOWN", f"{name}: {sid} is not a D1 signal"))
        elif kind[name] == "log" and sid not in LOG_SIGNALS:
            res.errors.append(("E-LOG-SIGNAL", f"{name}: {sid} is not a log signal"))
        elif kind[name] == "sample" and sid in LOG_SIGNALS:
            res.errors.append(("E-LOG-SIGNAL", f"{name}: {sid} is a log signal"))
        if kind[name] == "log":
            for field_name in ("logger", "template", "exc_type", "message"):
                _check_match(res, name, sig["log"].get(field_name))
            for match in sig["log"].get("args", {}).values():
                _check_match(res, name, match)

    # Params are self-consistent.
    for pname, spec in params.items():
        values = [spec["default"], spec["min"], spec["max"]]
        if spec["type"] == "duration":
            if not all(
                isinstance(v, str) and v[:-1].isdigit() and v[-1] in "smh"
                for v in values
            ):
                res.errors.append(
                    ("E-PARAM", f"{pname}: duration params need durations")
                )
                continue
            values = [seconds(v) for v in values]
        elif not all(isinstance(v, (int, float)) for v in values):
            res.errors.append(("E-PARAM", f"{pname}: number params need numbers"))
            continue
        lo, hi = values[1], values[2]
        if not lo <= values[0] <= hi:
            res.errors.append(("E-PARAM", f"{pname}: default outside [min, max]"))
        if spec["type"] == "duration" and hi > MAX_WINDOW_S:
            res.errors.append(("E-WINDOW", f"{pname}: max above 48h"))

    # Conditions: depth, count, references, fn/kind fit, windows.
    used: set[str] = set()
    conds = list(_walk(rule["when"], 1, res))
    if len(conds) > MAX_CONDITIONS:
        res.errors.append(("E-LIMIT", f"{len(conds)} conditions, max {MAX_CONDITIONS}"))
    for cond in conds:
        fn = cond["fn"]
        refs = [cond[k] for k in ("signal", "set", "clear") if k in cond]
        for ref in refs:
            if ref not in signals:
                res.errors.append(("E-REF", f"{fn}: no signal named {ref}"))
            used.add(ref)
        if fn in SAMPLE_FNS and kind.get(cond.get("signal")) == "log":
            res.errors.append(("E-FN-TYPE", f"{fn} needs a sample signal"))
        if fn in LOG_FNS and kind.get(cond.get("signal")) == "sample":
            res.errors.append(("E-FN-TYPE", f"{fn} needs a log signal"))
        if fn == "latch" and any(kind.get(r) == "sample" for r in refs):
            res.errors.append(("E-FN-TYPE", "latch set/clear must be log signals"))
        for key in ("window", "value"):
            if isinstance(cond.get(key), dict):
                p = cond[key]["param"]
                if p not in params:
                    res.errors.append(("E-REF", f"{fn}: no param named {p}"))
                elif key == "window" and params[p]["type"] != "duration":
                    res.errors.append(
                        ("E-PARAM", f"{p}: window param must be a duration")
                    )
        if (
            isinstance(cond.get("window"), str)
            and seconds(cond["window"]) > MAX_WINDOW_S
        ):
            res.errors.append(("E-WINDOW", f"{fn}: window above 48h"))
    if not res.errors:  # otherwise it's a knock-on of an earlier error
        for name in sorted(set(signals) - used):
            res.errors.append(("E-UNUSED", f"signal {name} is declared but never used"))
    for key in ("for", "keep_firing_for"):
        if key in rule and seconds(rule[key]) > MAX_FOR_S:
            res.errors.append(("E-WINDOW", f"{key} above 24h"))

    # group_by names must be produced by some signal.
    produced = set()
    for name, sig in signals.items():
        produced |= set(sig[kind[name]].get("by", [])) | set(
            sig[kind[name]].get("keys", {})
        )
    for g in rule.get("group_by", []):
        if g not in produced:
            res.errors.append(
                ("E-GROUP", f"group_by {g} is not a key or label of any signal")
            )

    # Derived input_trust (E1 4a, D3 3a; ENGINE_API.md §4).
    res.input_trust = derive_trust(rule, kind)
    res.detection = rule.get("detection", "event")
    # A condition that waits out a quiet gap is absence detection (ENGINE_API.md §2).
    # Declared, not derived (X1 ⚑4a): only a rule that says nothing is refused, so an
    # explicit `detection: event` stands. Negated conditions aren't absence.
    if "detection" not in rule and any(
        _is_absence(n) for n in _positive(rule.get("when", {}))
    ):
        res.errors.append(
            ("E-DETECTION", "absent_for or '== 0' needs detection: absence")
        )

    # Lane-1 gate (K1 T-05 (2)).
    if rule["lane"] == 1:
        if res.input_trust != "trusted":
            res.errors.append(("E-LANE1", "lane 1 rules must not read untrusted input"))
        if len(used) < 2 or not any(kind[n] == "sample" for n in used):
            res.errors.append(
                ("E-LANE1", "lane 1 needs >= 2 signals, at least one a sample")
            )

    # Engine version: too new is skipped and reported, not an error.
    major, minor = (int(x) for x in rule.get("engine_api", "1.0").split("."))
    if (major, minor) > ENGINE_API and not res.errors:
        res.skipped = (
            f"needs engine API {major}.{minor}, have {ENGINE_API[0]}.{ENGINE_API[1]}"
        )
    return res


def derive_trust(rule: dict, kind: dict) -> str:
    if rule.get("input_trust") == "untrusted":
        return "untrusted"
    for name, sig in rule["signals"].items():
        if kind[name] != "log":
            continue
        log = sig["log"]
        if log.get("trusted_only", True) is False or "message" in log or "args" in log:
            return "untrusted"
        if any(v.startswith("args[") for v in log.get("keys", {}).values()):
            return "untrusted"
    return "trusted"


def _walk(node: dict, depth: int, res: Result):
    if "fn" in node:
        yield node
        return
    if depth > MAX_DEPTH:
        res.errors.append(("E-DEPTH", f"nesting deeper than {MAX_DEPTH}"))
        return
    for key in ("all", "any"):
        for child in node.get(key, []):
            yield from _walk(child, depth + 1, res)
    if "not" in node:
        yield from _walk(node["not"], depth + 1, res)


def _positive(node: dict):
    """Condition leaves outside any `not` (a lint walk; depth is checked by _walk)."""
    if "fn" in node:
        yield node
        return
    for key in ("all", "any"):
        for child in node.get(key, []):
            yield from _positive(child)


def _is_absence(node: dict) -> bool:
    if node.get("fn") == "absent_for":
        return True
    return (
        node.get("fn") in ("increase", "rate", "changes", "count")
        and node.get("op") in ("==", "<=")
        and node.get("value") == 0
    )


def _check_match(res: Result, name: str, match: dict | None) -> None:
    if not match or "re2" not in match:
        return
    opts = re2.Options()
    opts.max_mem = 1 << 20
    try:
        compiled = re2.compile(match["re2"], options=opts)
    except re2.error as exc:
        res.errors.append(("E-RE2", f"{name}: not RE2 or too big: {exc}"))
        return
    if compiled.programsize > MAX_RE2_PROGRAM:
        res.errors.append(
            (
                "E-RE2",
                f"{name}: RE2 program size {compiled.programsize} > {MAX_RE2_PROGRAM}",
            )
        )
