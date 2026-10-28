"""The raw observations the engine has accepted, indexed by signal and sensor.

Only observations with observed_at ≤ the tick are ever added (semantics.md §1).
Latch state lives here too, but it is *persisted* state, not history: it survives
a restart and outlives the raw history (§2).
"""

from __future__ import annotations

import bisect
import functools
import json

from services.medic.engine.evaluate import (
    Reads,
    Series,
    line_group,
    line_matches,
    row_key,
    ts,
)


def gkey(group: dict) -> str:
    """A group's stable key: canonical JSON of its sorted (name, value) pairs."""
    pairs = tuple(sorted(group.items()))
    try:
        return _gkey(pairs)
    except TypeError:  # an unhashable value: not a label, but still a key
        return json.dumps(pairs, separators=(",", ":"))


@functools.lru_cache(maxsize=4096)
def _gkey(pairs: tuple) -> str:
    return json.dumps(pairs, separators=(",", ":"))


def sample_entries(spec: dict, obs: dict) -> list[dict]:
    want = spec.get("labels", {})
    return [
        v
        for v in obs.get("values", [])
        if v["key"] == spec["key"]
        and all(v.get("labels", {}).get(k) == x for k, x in want.items())
    ]


def _insort(rows: list, row: tuple) -> None:
    # Ordered by (t, id) whatever the arrival order, so live and replay agree.
    if not rows or row_key(rows[-1]) <= row_key(row):
        rows.append(row)  # the usual case: in order
    else:
        bisect.insort(rows, row, key=row_key)


def _upto(rows: list, now: float) -> list:
    """Rows at or before now; the list itself when that is all of them."""
    if not rows or rows[-1][0] <= now:
        return rows
    return rows[: bisect.bisect_right(rows, now, key=lambda r: r[0])]


class History:
    def __init__(self, latches: dict) -> None:
        self.samples: dict[str, list[tuple[float, dict]]] = {}  # by signal
        self.logs: dict[str, list[tuple[float, dict]]] = {}  # by signal
        self.beats: dict[str, list[tuple[float, dict]]] = {}  # by sensor
        self.interval: dict[str, float] = {}  # signal -> covering sensor's interval_s
        self.sensor_of: dict[str, str] = {}
        self.latches = (
            latches  # rule -> "set|clear" -> group key or "*" -> [set_t, clear_t]
        )
        # Indexes over self.samples, kept in its (t, id) order:
        self.failed: dict[str, list[tuple]] = {}  # signal -> (t, obs, None)
        self.ok_times: dict[str, list[float]] = {}  # signal -> t of each ok read
        self.series: dict[tuple[str, str], dict[str, Series]] = {}  # by labels
        self._memo: dict = {}  # this tick's reads; any add or prune clears it

    def add(self, obs: dict) -> None:
        self._memo.clear()
        t = ts(obs["t"])
        if obs["kind"] == "sensor_health":
            health = obs["health"]
            for signal in health["covers"]:
                self.interval[signal] = health["interval_s"]
                self.sensor_of[signal] = health["sensor"]
            _insort(self.beats.setdefault(health["sensor"], []), (t, obs))
            return
        signal = obs["signal"]
        if obs["kind"] == "log":
            _insort(self.logs.setdefault(signal, []), (t, obs))
            return
        _insort(self.samples.setdefault(signal, []), (t, obs))
        if obs["outcome"] != "ok":
            _insort(self.failed.setdefault(signal, []), (t, obs, None))
            return
        bisect.insort(self.ok_times.setdefault(signal, []), t)
        seen = set()
        for v in obs.get("values", []):
            series = (v["key"], gkey(v.get("labels", {})))
            if series not in seen:  # a series' first entry in a read is its value
                seen.add(series)
                by_labels = self.series.setdefault((signal, series[0]), {})
                if series[1] not in by_labels:
                    by_labels[series[1]] = Series()
                by_labels[series[1]].add(t, obs, v)

    def prune(self, before: float) -> None:
        """Drop reads older than every window still needs (keeps each newest one)."""
        self._memo.clear()
        for index in (self.logs, self.beats):
            for rows in index.values():
                del rows[: _cut(rows, before)]
        cutoff = {}  # signal -> (t, id) of its oldest kept read
        for signal, rows in self.samples.items():
            i = _cut(rows, before)
            if i:
                failed = sum(obs["outcome"] != "ok" for _, obs in rows[:i])
                del self.failed.get(signal, [])[:failed]
                del self.ok_times.get(signal, [])[: i - failed]
                del rows[:i]
                cutoff[signal] = row_key(rows[0])
        for (signal, _), by_labels in self.series.items():
            if signal in cutoff:
                for labels, series in list(by_labels.items()):
                    rows = series.rows
                    series.drop(bisect.bisect_left(rows, cutoff[signal], key=row_key))
                    if not series.rows:
                        del by_labels[labels]

    def _memoized(self, key: tuple, make):
        if key not in self._memo:
            self._memo[key] = make()
        return self._memo[key]

    def sample_reads(self, spec: dict, group_by: list, group: dict, now: float):
        """The reads of this group's series: failed reads, plus ok reads that carry
        its value (present or absent). An ok read without it isn't a read of this
        series: a source that disappeared goes stale, it doesn't read absent (§4).
        None when a shared signal has more than one series at its newest read (§3).
        Every rule and group reading the same series this tick shares one Reads."""
        produces = bool(group_by) and set(group_by) <= set(spec.get("by", []))
        want = spec.get("labels", {})
        pin = tuple((n, group[n]) for n in group_by) if produces else ()
        key = ("sample", spec["signal"], spec["key"], gkey(want), pin, now)
        return self._memoized(key, lambda: self._sample_reads(spec, want, pin, now))

    def _sample_reads(self, spec: dict, want: dict, pin: tuple, now: float):
        signal, names = spec["signal"], tuple(n for n, _ in pin)
        by_group = self._memoized(
            ("series", signal, spec["key"], gkey(want), names, now),
            lambda: self._series_by(signal, spec["key"], want, names, now),
        )
        found = by_group.get(tuple(v for _, v in pin), [])
        newest = max(found, key=lambda f: row_key(f[0].rows[f[1] - 1]), default=None)
        if newest:
            read = newest[0].rows[newest[1] - 1][1]
            if sum(s.rows[hi - 1][1] is read for s, hi in found) > 1:
                return None  # ambiguous_series: unknown
        signal_ok = self._memoized(
            ("ok", signal, now),
            lambda: _upto_times(self.ok_times.get(signal, []), now),
        )
        failed = _upto(self.failed.get(signal, []), now)
        return Reads(newest, failed, self.interval.get(signal), signal_ok)

    def _series_by(self, signal: str, key: str, want: dict, names: tuple, now: float):
        """(series, how many of its reads are at or before now) for each series that
        matches `want` and has such a read, by its `names` values."""
        out: dict[tuple, list] = {}
        for series in self.series.get((signal, key), {}).values():
            labels = series.rows[0][2].get("labels", {})
            hi = series.upto(now)
            if hi and all(labels.get(k) == x for k, x in want.items()):
                out.setdefault(tuple(labels.get(n) for n in names), []).append(
                    (series, hi)
                )
        return out

    def log_reads(self, spec: dict, group_by: list, group: dict, now: float):
        """(matching lines, ok heartbeat times, interval) for this group."""
        produces = bool(group_by) and set(group_by) <= set(spec.get("keys", {}))
        text = json.dumps(spec, sort_keys=True)
        lines = self._memoized(
            ("lines", text, now),
            lambda: [
                (t, obs, None)
                for t, obs in self.logs.get(spec["signal"], [])
                if t <= now and line_matches(spec, obs)
            ],
        )
        if produces:
            lines = [
                r
                for r in lines
                if all(
                    (line_group(spec, r[1]) or {}).get(n) == group[n] for n in group_by
                )
            ]
        sensor = self.sensor_of.get(spec["signal"])
        beats = self._memoized(
            ("beats", sensor, now),
            lambda: [
                t
                for t, obs in self.beats.get(sensor, [])
                if t <= now and obs["health"]["state"] == "ok"
            ],
        )
        return lines, beats, self.interval.get(spec["signal"])

    def note_latch(
        self, rule_id: str, node: dict, which: int, key: str, t: float
    ) -> None:
        slots = self.latches.setdefault(rule_id, {}).setdefault(_latch_key(node), {})
        slot = slots.setdefault(key, [None, None])
        slot[which] = t if slot[which] is None else max(slot[which], t)

    def latch(self, rule_id: str, node: dict, group: dict):
        slots = self.latches.get(rule_id, {}).get(_latch_key(node), {})
        both = [slots.get(gkey(group), [None, None]), slots.get("*", [None, None])]
        newest = [
            max((s[i] for s in both if s[i] is not None), default=None) for i in (0, 1)
        ]
        return newest[0], newest[1]


def _latch_key(node: dict) -> str:
    return f"{node['set']}|{node['clear']}"


def _cut(rows: list, before: float) -> int:
    """How many of the oldest rows are older than `before`, keeping the newest."""
    i = bisect.bisect_left(rows, before, key=lambda r: r[0])
    return min(i, len(rows) - 1) if rows else 0


def _upto_times(times: list[float], now: float) -> list[float]:
    return (
        times
        if not times or times[-1] <= now
        else times[: bisect.bisect_right(times, now)]
    )
