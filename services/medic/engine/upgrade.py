"""Upgrade windows (semantics.md §7): while one is open, pending can't promote.

A window opens when the `version` signal changes, or when restart markers for two or
more Vigil services fall within 5 min of each other (a counter epoch change, a
`…started_at` change, an `…uptime_seconds` drop). It lasts 15 min after the last
such marker. Everything here is persisted state: the last value of each marker
series, each service's newest restart, and the window's end.
"""

from __future__ import annotations

from services.medic.engine.history import gkey

WINDOW_S = 900  # 15 min after the last restart (§7, ⚑ 4)
TOGETHER_S = 300  # restarts of 2+ services within 5 min of each other
NOT_VIGIL = {"medic", "medic-gateway", "host"}  # their restarts aren't an upgrade


class UpgradeWatch:
    def __init__(self, state: dict | None) -> None:
        state = state or {}
        self.until: float | None = state.get("until")
        self.marks: dict[str, list] = state.get("marks", {})  # series -> [t, value]
        self.restarts: dict[str, float] = state.get("restarts", {})  # service -> t

    def state(self) -> dict:
        return {"until": self.until, "marks": self.marks, "restarts": self.restarts}

    def active(self, now: float) -> bool:
        return self.until is not None and now < self.until

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
        self.until = max(self.until or t, t + WINDOW_S)


def _number(v) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)
