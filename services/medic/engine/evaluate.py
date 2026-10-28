"""Three-valued evaluation of one rule × group at one tick (semantics.md §2).

Truth is True, False or UNKNOWN (None). Windows are measured on each observation's
`t`; an incompletely covered window gives a lower bound that decides a comparison
only when every larger value agrees. Nothing here reads a clock or does I/O.
"""

from __future__ import annotations

import bisect
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
    holds = _cmp(op, x, value)
    if holds is UNKNOWN or covered:
        return holds
    if op in (">", ">="):  # once true for x, true for every larger y
        return True if holds else UNKNOWN
    if op in ("==", "!="):  # decided only once x is already past the value
        if not isinstance(value, int | float):
            return UNKNOWN
        return (op == "!=") if x > value else UNKNOWN
    return UNKNOWN if holds else False  # < and <=: once false, false for every y


def _cmp(op: str, x, value) -> bool | None:
    """A comparison; values of types that don't compare (text vs number) are unknown."""
    try:
        return OPS[op](x, value)
    except TypeError:
        return UNKNOWN


def _number(v) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


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


def row_key(row: tuple) -> tuple:
    return row[0], row[1]["id"]


class Series:
    """One series' ok reads, by (t, id), with the columns a window scans kept beside
    them: reading every entry of a 48 h window each tick costs more than the rest."""

    __slots__ = ("epochs", "present", "rows", "times", "untrusted", "values")

    def __init__(self) -> None:
        self.rows: list[tuple] = []  # (t, observation, this series' entry)
        self.times: list[float] = []
        self.values: list = []
        self.epochs: list = []
        self.present: list[bool] = []
        self.untrusted: list[bool] = []

    def _columns(self) -> tuple[list, ...]:
        return (
            self.rows,
            self.times,
            self.values,
            self.epochs,
            self.present,
            self.untrusted,
        )

    def add(self, t: float, obs: dict, entry: dict) -> None:
        row, rows = (t, obs, entry), self.rows
        # Ordered by (t, id) whatever the arrival order, so live and replay agree.
        cells = (
            row,
            t,
            entry.get("value"),
            obs.get("epoch"),
            entry.get("state") == "present",
            entry.get("trust") == "untrusted",
        )
        if not rows or row_key(rows[-1]) <= row_key(row):  # the usual case: in order
            for column, cell in zip(self._columns(), cells):
                column.append(cell)
            return
        i = bisect.bisect_right(rows, row_key(row), key=row_key)
        for column, cell in zip(self._columns(), cells):
            column.insert(i, cell)

    def drop(self, n: int) -> None:
        """Forget the n oldest reads."""
        for column in self._columns():
            del column[:n]

    def upto(self, now: float) -> int:
        """How many reads are at or before now."""
        times = self.times
        return (
            len(times)
            if not times or times[-1] <= now
            else bisect.bisect_right(times, now)
        )


class Reads:
    """One signal's reads up to tick T, for one group (or shared by every group):
    its series' first `hi` reads, and the signal's failed reads. Shared by every rule
    that reads the series this tick, so callers don't mutate it; `memo` holds what
    they derive from it."""

    def __init__(self, found, failed: list, interval, signal_ok: list[float]):
        self.series, self.hi = found or (None, 0)  # found: (Series, hi) or None
        self.failed = failed  # (t, observation, None), by (t, id)
        self.interval = interval
        self.signal_ok = signal_ok  # times of every ok read of the signal
        self.memo: dict = {}

    def newest_ok(self):
        return self.series.rows[self.hi - 1] if self.hi else None

    def last(self):
        """The newest read, ok or failed."""
        rows = [r for r in (self.newest_ok(), *self.failed[-1:]) if r]
        return max(rows, key=row_key, default=None)

    def fresh(self, now: float) -> bool:
        return (
            self.interval is not None
            and self.hi > 0
            and self.newest_ok()[0] >= now - 2 * self.interval
        )


def covered(times: list[float], start: float, now: float, gap: float) -> bool:
    """Reads (or heartbeats) never more than `gap` apart from `start` through now.
    `times` is sorted."""
    points = times[bisect.bisect_left(times, start) :]
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
        self.untrusted = False  # a `trust: untrusted` value was used (ENGINE_API §5)

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
        self.taint(read)

    def taint(self, read) -> None:
        if read[2] is not None and read[2].get("trust") == "untrusted":
            self.untrusted = True


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
    if reads is None or not reads.fresh(ctx.now) or reads.last()[1]["outcome"] != "ok":
        return UNKNOWN  # stale, or the newest read failed
    read = reads.newest_ok()
    ctx.note(node["signal"], read)
    if read[2] is None or read[2]["state"] != "present":
        return False  # absent is a known fact (§2)
    x = read[2]["value"]
    if age:
        try:
            x = ctx.now - ts(x)
        except (TypeError, ValueError):  # not a timestamp: can't evaluate
            return UNKNOWN
    return _cmp(node["op"], x, ctx.value(node["value"]))


def _from_baseline(base: float | None, times: list[float], lo: float, ctx, gap) -> bool:
    """Fully covered (§2): a baseline no older than lo − gap, then no gap > `gap`."""
    return base is not None and base >= lo - gap and covered(times, base, ctx.now, gap)


def _window(node, ctx):
    """(W, values of [baseline] + present reads in (T − W, T], their epochs, fully
    covered, value type, memo for what callers derive from them), or None if stale."""
    reads = ctx.samples(node["signal"])
    if reads is None or not reads.fresh(ctx.now):
        return None
    w = ctx.window(node)
    if w not in reads.memo:  # the same for every rule reading this series and window
        s, hi, gap, lo = reads.series, reads.hi, 2 * reads.interval, ctx.now - w
        j = bisect.bisect_left(s.times, lo - gap, 0, hi)  # no baseline is older
        keep = s.present[j:hi]
        everything = all(keep)

        def present(column: list) -> list:
            part = column[j:hi]
            return part if everything else list(itertools.compress(part, keep))

        rows, times = present(s.rows), present(s.times)
        i = bisect.bisect_right(times, lo)
        k, n = max(i - 1, 0), len(rows)  # rows[k:i] is the baseline, rows[i:] inside
        end = n - 1 if i < n else n  # tainted by all but the newest inside (it's noted)
        taints = list(itertools.compress(rows[k:end], present(s.untrusted)[k:end]))
        full = i > 0 and _from_baseline(times[k], s.times[j:hi], lo, ctx, gap)
        values, epochs = present(s.values)[k:], present(s.epochs)[k:]
        vtype = s.rows[hi - 1][2]["type"]
        reads.memo[w] = (values, epochs, full, vtype, taints, rows[i:][-1:], {})
    values, epochs, full, vtype, taints, newest, memo = reads.memo[w]
    for r in taints:
        ctx.taint(r)
    for r in newest:
        ctx.note(node["signal"], r)
    return w, values, epochs, full, vtype, memo


def _increase(node, ctx, per_second: bool = False):
    got = _window(node, ctx)
    if got is None:
        return UNKNOWN
    w, values, epochs, full, vtype, memo = got
    if "increase" not in memo:
        if not all(map(_number, values)):
            memo["increase"] = UNKNOWN  # not a number: can't evaluate
        elif vtype != "counter":  # a gauge's rise has no lower bound
            memo["increase"] = values[-1] - values[0] if full else UNKNOWN
        else:  # a counter that restarted from 0 adds its new value
            memo["increase"] = sum(
                (b - a if b > a else 0) if ea == eb else b
                for a, b, ea, eb in zip(values, values[1:], epochs, epochs[1:])
            )
    x = memo["increase"]
    if x is UNKNOWN:
        return UNKNOWN
    x = x / w if per_second else x
    return decide(node["op"], x, ctx.value(node["value"]), full)


def _changes(node, ctx):
    got = _window(node, ctx)
    if got is None:
        return UNKNOWN
    _, values, epochs, full, vtype, memo = got
    if "changes" not in memo:
        counter = vtype == "counter"  # changes only grow with more reads: any type (§2)
        epochs = epochs if counter else [None] * len(values)
        memo["changes"] = sum(  # a counter's restart is neither a change nor flat
            a != b
            for a, b, ea, eb in zip(values, values[1:], epochs, epochs[1:])
            if ea == eb
        )
    return decide(node["op"], memo["changes"], ctx.value(node["value"]), full)


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
        s, hi = reads.series, reads.hi
        j = bisect.bisect_right(s.times, ctx.now - w, 0, hi) if s else 0
        seen = s.rows[j:hi] if s else []
        hits = list(itertools.compress(seen, s.present[j:hi])) if s else []
        # Covered by the signal's ok reads (§2): a source that vanished from a
        # sensor that still reads is absent, not unknown.
        times, interval = reads.signal_ok, reads.interval
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
