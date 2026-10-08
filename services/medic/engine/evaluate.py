"""Three-valued evaluation of one rule × group at one tick (semantics.md §2).

Truth is True, False or UNKNOWN (None). Windows are measured on each observation's
`t`; an incompletely covered window gives a lower bound that decides a comparison
only when every larger value agrees. Nothing here reads a clock or does I/O.
"""

from __future__ import annotations

import functools
import itertools
import operator
import re
from datetime import datetime

import re2

from services.medic.contracts.fingerprint_ref import label_safe
from services.medic.contracts.rule_check import seconds

UNKNOWN = None
OPS = {
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}
_ID = re.compile(r"^[A-Za-z0-9_.:/@+-]{1,128}$")


def k_all(values) -> bool | None:
    values = list(values)
    if any(v is False for v in values):
        return False
    return UNKNOWN if any(v is UNKNOWN for v in values) else True


def k_any(values) -> bool | None:
    values = list(values)
    if any(v is True for v in values):
        return True
    return UNKNOWN if any(v is UNKNOWN for v in values) else False


def k_not(value: bool | None) -> bool | None:
    return UNKNOWN if value is UNKNOWN else not value


def ts(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


def decide(op: str, x: float, value, covered: bool) -> bool | None:
    """Compare x, or, when x is only a lower bound, decide only if every y ≥ x agrees."""
    holds = OPS[op](x, value)
    if covered:
        return holds
    if op in (">", ">=", "!="):  # once true for x, true for every larger y
        return True if holds else UNKNOWN
    if op == "==":  # fails for every y ≥ x only when x is already past it
        return False if x > value else UNKNOWN
    return UNKNOWN if holds else False  # < and <=: once false, false for every y


@functools.lru_cache(maxsize=512)
def _re2(pattern: str):
    opts = re2.Options()
    opts.max_mem = 1 << 20
    return re2.compile(pattern, options=opts)


def _match(m: dict, text) -> bool:
    if not isinstance(text, str):
        return False
    if "eq" in m:
        return text == m["eq"]
    if "prefix" in m:
        return text.startswith(m["prefix"])
    return _re2(m["re2"]).search(text) is not None


def line_matches(spec: dict, obs: dict) -> bool:
    log = obs["log"]
    if spec.get("trusted_only", True) and log.get("template_trust") != "catalog":
        return False
    if "level" in spec and log.get("level") not in spec["level"]:
        return False
    if "fingerprint" in spec and log.get("fingerprint") != spec["fingerprint"]:
        return False
    for name in ("logger", "template", "exc_type", "message"):
        if name in spec and not _match(spec[name], log.get(name)):
            return False
    args = log.get("args", [])
    return all(
        int(i) < len(args) and _match(m, args[int(i)])
        for i, m in spec.get("args", {}).items()
    )


def line_group(spec: dict, obs: dict) -> dict[str, str] | None:
    """Group values a line gives through `keys`, label-safe (semantics.md §3)."""
    out = {}
    for name, source in spec.get("keys", {}).items():
        raw = _key_source(obs["log"], source)
        if not isinstance(raw, str) or not raw:
            return None
        out[name] = label_safe(raw)
    return out


def _key_source(log: dict, source: str):
    if not source.startswith("args["):
        return log.get(source)
    args, i = log.get("args", []), int(source[5])
    return args[i] if i < len(args) else None


class Reads:
    """One signal's reads up to tick T, for one group (or shared by every group)."""

    def __init__(
        self, items: list[tuple[float, dict, dict | None]], interval: float | None
    ):
        self.items = items  # (t, observation, matching value entry or None), by t
        self.interval = interval

    def ok(self):
        return [r for r in self.items if r[1]["outcome"] == "ok"]

    def present(self):
        return [r for r in self.ok() if r[2] is not None and r[2]["state"] == "present"]

    def fresh(self, now: float) -> bool:
        ok = self.ok()
        return (
            self.interval is not None
            and bool(ok)
            and ok[-1][0] >= now - 2 * self.interval
        )


def covered(times: list[float], start: float, now: float, gap: float) -> bool:
    """Reads (or heartbeats) never more than `gap` apart from `start` through now."""
    points = [t for t in times if t >= start]
    if not points or points[0] > start + gap:
        return False
    points.append(now)
    return all(b - a <= gap for a, b in itertools.pairwise(points))


class Context:
    """One evaluation: the rule, the group, the tick, and the evidence it used."""

    def __init__(self, rule, group: dict, now: float, history) -> None:
        self.rule, self.group, self.now, self.history = rule, group, now, history
        self.group_by = rule.rule.get("group_by", [])
        self.evidence: dict[str, tuple[float, dict, dict | None]] = {}

    def value(self, v):
        return self.rule.param(v["param"]) if isinstance(v, dict) else v

    def window(self, node: dict) -> float:
        w = node["window"]
        return self.rule.param(w["param"]) if isinstance(w, dict) else seconds(w)

    def samples(self, name: str) -> Reads | None:
        spec = self.rule.rule["signals"][name]["sample"]
        return self.history.sample_reads(spec, self.group_by, self.group, self.now)

    def logs(self, name: str):
        spec = self.rule.rule["signals"][name]["log"]
        return self.history.log_reads(spec, self.group_by, self.group, self.now)

    def note(self, name: str, read) -> None:
        """Evidence: per signal, the newest observation the evaluation used (G2)."""
        if name not in self.evidence or read[0] >= self.evidence[name][0]:
            self.evidence[name] = read


def evaluate(node: dict, ctx: Context) -> bool | None:
    if "all" in node:
        return k_all([evaluate(n, ctx) for n in node["all"]])
    if "any" in node:
        return k_any([evaluate(n, ctx) for n in node["any"]])
    if "not" in node:
        return k_not(evaluate(node["not"], ctx))
    return _FNS[node["fn"]](node, ctx)


def _latest(node, ctx, age: bool = False):
    reads = ctx.samples(node["signal"])
    if (
        reads is None
        or not reads.fresh(ctx.now)
        or reads.items[-1][1]["outcome"] != "ok"
    ):
        return UNKNOWN  # stale, or the newest read failed
    read = reads.items[-1]
    ctx.note(node["signal"], read)
    if read[2] is None or read[2]["state"] != "present":
        return False  # absent is a known fact (§2)
    x = ctx.now - ts(read[2]["value"]) if age else read[2]["value"]
    return OPS[node["op"]](x, ctx.value(node["value"]))


def _from_baseline(base: float | None, times: list[float], lo: float, ctx, gap) -> bool:
    """Fully covered (§2): a baseline no older than lo − gap, then no gap > `gap`."""
    return base is not None and base >= lo - gap and covered(times, base, ctx.now, gap)


def _window(node, ctx):
    """(W, [baseline] + present reads in (T − W, T], fully covered) or None if stale."""
    reads = ctx.samples(node["signal"])
    if reads is None or not reads.fresh(ctx.now):
        return None
    w, gap = ctx.window(node), 2 * reads.interval
    lo, present = ctx.now - w, reads.present()
    before = [r for r in present if r[0] <= lo and r[0] >= lo - gap][-1:]
    inside = [r for r in present if r[0] > lo]
    if inside:
        ctx.note(node["signal"], inside[-1])
    oks = [r[0] for r in reads.ok()]
    full = _from_baseline(before[0][0] if before else None, oks, lo, ctx, gap)
    return w, before + inside, full


def _same_epoch(a, b) -> bool:
    return a[1].get("epoch") == b[1].get("epoch")


def _rise(a, b) -> float:
    """A counter's rise between reads; one that restarted from 0 adds its new value."""
    if _same_epoch(a, b):
        return max(0, b[2]["value"] - a[2]["value"])
    return b[2]["value"]


def _increase(node, ctx, per_second: bool = False):
    got = _window(node, ctx)
    if got is None:
        return UNKNOWN
    w, seq, full = got
    if seq and seq[-1][2]["type"] != "counter":
        if not full:
            return UNKNOWN  # a gauge's rise has no lower bound
        x = seq[-1][2]["value"] - seq[0][2]["value"]
    else:
        x = sum(_rise(a, b) for a, b in itertools.pairwise(seq))
    x = x / w if per_second else x
    return decide(node["op"], x, ctx.value(node["value"]), full)


def _changes(node, ctx):
    got = _window(node, ctx)
    if got is None:
        return UNKNOWN
    _, seq, full = got
    counter = bool(seq) and seq[-1][2]["type"] == "counter"
    if not full and not counter:
        return UNKNOWN  # lower bounds apply to changes on counters only
    x = sum(
        a[2]["value"] != b[2]["value"]
        for a, b in itertools.pairwise(seq)
        if not counter or _same_epoch(a, b)  # a restart is neither a change nor flat
    )
    return decide(node["op"], x, ctx.value(node["value"]), full)


def _count(node, ctx):
    lines, beats, interval = ctx.logs(node["signal"])
    w = ctx.window(node)
    lines = [r for r in lines if r[0] > ctx.now - w]
    if lines:
        ctx.note(node["signal"], lines[-1])
    x = len({r[1]["log"]["line_hash"] for r in lines})
    full = False
    if interval is not None:
        lo = ctx.now - w
        base = max((t for t in beats if t <= lo), default=None)
        full = _from_baseline(base, beats, lo, ctx, 2 * interval)
    return decide(node["op"], x, ctx.value(node["value"]), full)


def _absent_for(node, ctx):
    w = ctx.window(node)
    if "log" in ctx.rule.rule["signals"][node["signal"]]:
        lines, times, interval = ctx.logs(node["signal"])
        hits, seen = [r for r in lines if r[0] > ctx.now - w], []
    else:
        reads = ctx.samples(node["signal"])
        if reads is None:
            return UNKNOWN
        seen = [r for r in reads.ok() if r[0] > ctx.now - w]
        hits = [r for r in seen if r[2] is not None and r[2]["state"] == "present"]
        times, interval = [r[0] for r in seen], reads.interval
    if hits or seen:
        ctx.note(node["signal"], (hits or seen)[-1])
    if hits:
        return False
    if interval is None:
        return UNKNOWN
    return True if covered(times, ctx.now - w, ctx.now, 2 * interval) else UNKNOWN


def _latch(node, ctx):
    set_t, clear_t = ctx.history.latch(ctx.rule.id, node, ctx.group)
    for name in (node["set"], node["clear"]):
        lines = ctx.logs(name)[0]
        if lines:
            ctx.note(name, lines[-1])
    return set_t is not None and (clear_t is None or set_t > clear_t)  # tie: clear


_FNS = {
    "latest": _latest,
    "age": functools.partial(_latest, age=True),
    "increase": _increase,
    "rate": functools.partial(_increase, per_second=True),
    "changes": _changes,
    "count": _count,
    "absent_for": _absent_for,
    "latch": _latch,
}
