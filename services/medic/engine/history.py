"""The raw observations the engine has accepted, indexed by signal and sensor.

Only observations with observed_at ≤ the tick are ever added (semantics.md §1).
Latch state lives here too, but it is *persisted* state, not history: it survives
a restart and outlives the raw history (§2).
"""

from __future__ import annotations

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
        series.append((ts(obs["t"]), obs))
        if len(series) > 1 and series[-2][0] > series[-1][0]:  # a late line
            series.sort(key=lambda r: (r[0], r[1]["id"]))

    def prune(self, before: float) -> None:
        """Drop reads older than every window still needs (keeps each newest one)."""
        for index in (self.samples, self.logs, self.beats):
            for series in index.values():
                i = 0
                while i < len(series) - 1 and series[i][0] < before:
                    i += 1
                del series[:i]

    def sample_reads(self, spec: dict, group_by: list, group: dict, now: float):
        """The signal's reads for this group; None when the series is ambiguous (§3)."""
        produces = bool(group_by) and set(group_by) <= set(spec.get("by", []))
        items, series = [], set()
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
            series.update(gkey(v.get("labels", {})) for v in found)
            items.append((t, obs, found[0] if found else None))
        if len(series) > 1:
            return None  # ambiguous_series: unknown
        return Reads(items, self.interval.get(spec["signal"]))

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
