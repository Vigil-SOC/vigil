"""The raw observations the engine has accepted, indexed by signal and sensor.

Only observations with observed_at ≤ the tick are ever added (semantics.md §1).
Latch state lives here too, but it is *persisted* state, not history: it survives
a restart and outlives the raw history (§2).
"""

from __future__ import annotations

import bisect
import json

from services.medic.engine.evaluate import Reads, line_group, line_matches, ts


def gkey(group: dict) -> str:
    """A group's stable key: canonical JSON of its sorted (name, value) pairs."""
    return json.dumps(sorted(group.items()), separators=(",", ":"))


def sample_entries(spec: dict, obs: dict) -> list[dict]:
    want = spec.get("labels", {})
    return [
        v
        for v in obs.get("values", [])
        if v["key"] == spec["key"]
        and all(v.get("labels", {}).get(k) == x for k, x in want.items())
    ]


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

    def add(self, obs: dict) -> None:
        if obs["kind"] == "sensor_health":
            health = obs["health"]
            for signal in health["covers"]:
                self.interval[signal] = health["interval_s"]
                self.sensor_of[signal] = health["sensor"]
            series = self.beats.setdefault(health["sensor"], [])
        else:
            index = self.logs if obs["kind"] == "log" else self.samples
            series = index.setdefault(obs["signal"], [])
        # Ordered by (t, id) whatever the arrival order, so live and replay agree.
        bisect.insort(series, (ts(obs["t"]), obs), key=lambda r: (r[0], r[1]["id"]))

    def prune(self, before: float) -> None:
        """Drop reads older than every window still needs (keeps each newest one)."""
        for index in (self.samples, self.logs, self.beats):
            for series in index.values():
                i = 0
                while i < len(series) - 1 and series[i][0] < before:
                    i += 1
                del series[:i]

    def sample_reads(self, spec: dict, group_by: list, group: dict, now: float):
        """The reads of this group's series: failed reads, plus ok reads that carry
        its value (present or absent). An ok read without it isn't a read of this
        series: a source that disappeared goes stale, it doesn't read absent (§4).
        None when a shared signal has more than one series at its newest read (§3)."""
        produces = bool(group_by) and set(group_by) <= set(spec.get("by", []))
        rows = []
        for t, obs in self.samples.get(spec["signal"], []):
            if t > now:
                break
            found = sample_entries(spec, obs) if obs["outcome"] == "ok" else []
            if produces:
                found = [
                    v
                    for v in found
                    if all(v["labels"].get(n) == group[n] for n in group_by)
                ]
            rows.append((t, obs, found))
        newest = next((f for _, o, f in reversed(rows) if f), [])
        if len({gkey(v.get("labels", {})) for v in newest}) > 1:
            return None  # ambiguous_series: unknown
        series = gkey(newest[0].get("labels", {})) if newest else None
        items = []
        for t, obs, found in rows:
            entry = next(
                (v for v in found if gkey(v.get("labels", {})) == series), None
            )
            if obs["outcome"] != "ok" or entry is not None:
                items.append((t, obs, entry))
        oks = [t for t, obs, _ in rows if obs["outcome"] == "ok"]
        return Reads(items, self.interval.get(spec["signal"]), oks)

    def log_reads(self, spec: dict, group_by: list, group: dict, now: float):
        """(matching lines, ok heartbeat times, interval) for this group."""
        produces = bool(group_by) and set(group_by) <= set(spec.get("keys", {}))
        lines = []
        for t, obs in self.logs.get(spec["signal"], []):
            if t > now or not line_matches(spec, obs):
                continue
            if produces:
                got = line_group(spec, obs) or {}
                if any(got.get(n) != group[n] for n in group_by):
                    continue
            lines.append((t, obs, None))
        sensor = self.sensor_of.get(spec["signal"])
        beats = [
            t
            for t, obs in self.beats.get(sensor, [])
            if t <= now and obs["health"]["state"] == "ok"
        ]
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
