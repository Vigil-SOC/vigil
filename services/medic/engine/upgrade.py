"""Upgrade windows (semantics.md §7): while one is open, pending can't promote.

A window opens when the `version` signal changes, or when restart markers for two or
more Vigil services fall within 5 min of each other (a counter epoch change, a
`…started_at` change, an `…uptime_seconds` drop). It lasts 15 min after the last
such marker, but a chain of windows (each marker before the window it extends has
closed) holds for at most 60 min after the chain's first marker, sensor-blind
included (⚑ S4b2-6). Everything here is persisted state: the last value of each
marker series, each service's newest restart, the window's end and its chain's
start.
"""

from __future__ import annotations

from services.medic.engine.history import gkey

WINDOW_S = 900  # 15 min after the last restart (§7, ⚑ 4)
CAP_S = 3600  # a chain of windows holds no longer than this after its first marker
TOGETHER_S = 300  # restarts of 2+ services within 5 min of each other
NOT_VIGIL = {"medic", "medic-gateway", "host"}  # their restarts aren't an upgrade


class UpgradeWatch:
    def __init__(self, state: dict | None) -> None:
        state = state or {}
        self.until: float | None = state.get("until")  # the chain's uncapped end
        self.since: float | None = state.get("since")  # the chain's first marker
        if self.until is not None and self.since is None:  # state from before the cap
            self.since = self.until - WINDOW_S
        self.marks: dict[str, list] = state.get("marks", {})  # series -> [t, value]
        self.restarts: dict[str, float] = state.get("restarts", {})  # service -> t

    def state(self) -> dict:
        return {"until": self.until, "since": self.since} | {
            "marks": self.marks,
            "restarts": self.restarts,
        }

    @property
    def end(self) -> float | None:
        """When the hold ends: 15 min after the last marker, capped (§7)."""
        if self.until is None:
            return None
        return min(self.until, self.since + CAP_S)

    def active(self, now: float) -> bool:
        return self.end is not None and now < self.end

    def observe(self, obs: dict, t: float) -> None:
        if obs["kind"] != "sample" or obs["outcome"] != "ok":
            return
        restarted = False
        if obs.get("epoch") is not None:
            restarted |= self._changed(f"epoch|{obs['signal']}", t, obs["epoch"])
        for v in obs.get("values", []):
            if v.get("state") != "present":
                continue
            series = f"{obs['signal']}|{v['key']}|{gkey(v.get('labels', {}))}"
            value = v["value"]
            if obs["signal"] == "version":
                if self._changed(series, t, value):
                    self._open(t)
            elif v["key"].endswith("started_at"):
                restarted |= self._changed(series, t, value)
            elif v["key"].endswith("uptime_seconds") and _number(value):
                before = self.marks.get(series)
                restarted |= self._changed(series, t, value) and value < before[1]
        if restarted and obs["target"]["service"] not in NOT_VIGIL:
            self._restart(obs["target"]["service"], t)

    def _changed(self, series: str, t: float, value) -> bool:
        """Record the newest value; a late read (older than the newest) is ignored."""
        before = self.marks.get(series)
        if before is not None and t <= before[0]:
            return False
        self.marks[series] = [t, value]
        return before is not None and before[1] != value

    def _restart(self, service: str, t: float) -> None:
        self.restarts[service] = max(t, self.restarts.get(service, t))
        if any(
            s != service and abs(t - u) <= TOGETHER_S for s, u in self.restarts.items()
        ):
            self._open(t)

    def _open(self, t: float) -> None:
        if self.until is None or t >= self.until:  # the last window closed: new chain
            self.since = t
        else:  # a late marker in a live chain: the cap counts from the earliest
            self.since = min(self.since, t)
        self.until = max(self.until or t, t + WINDOW_S)


def _number(v) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)
