"""Sample Medic's containers from outside the process, and check its hard floor.

`stack.usage()` (each container's cumulative CPU counter, read twice a couple of
seconds apart, and its memory in use: the Docker API on Compose's medic, medic-gateway
and medic-dockerproxy, `crictl stats` on kind's medic and medic-gateway), once an
interval, for as long as the sampler runs; `du -sk` of the store at the start and the
end. Usage is reported against the advisory bars and never turns a run red.

The hard floor is `stack.medic_limits()` on the medic container alone: red when its
limit is not 1 vCPU / 1 GiB at the end (missing, raised or lowered), or when it was
killed for memory or restarted while sampled. Under the limit Medic can't go over it,
so sampled usage is not compared with it.
"""

from __future__ import annotations

import math
import threading
import time

STORE_PATH = "/var/lib/vigil-medic"
SERVICES = ("medic", "medic-gateway", "medic-dockerproxy")
# The epic's pass bars: reported, never red.
ADVISORY = {
    "cpu_p95_vcpu": 0.5,
    "peak_rss_mib": 512.0,
    "store_growth_mib_per_day": 5.0,
    "store_growth_mib_per_day_busy": 12.0,
}
# The limit the medic container must run under (Compose cpus / mem_limit, the chart's limits).
HARD_CPU_VCPU = 1.0
HARD_RSS_MIB = 1024.0


def floor_failures(start: dict | None, end: dict | None) -> list[str]:
    """The hard floor, from `stack.medic_limits()` when sampling started and when it stopped."""
    if not (start and end):
        return [f"Medic's limits could not be read at the {'end' if start else 'start'} of the run"]
    out = []
    for key, what, want, unit in (("cpu_vcpu", "CPU", HARD_CPU_VCPU, "vCPU"), ("memory_mib", "memory", HARD_RSS_MIB, "MiB")):
        got = end[key]
        if got is None:
            out.append(f"Medic runs with no {what} limit (want {want:g} {unit})")
        elif not math.isclose(got, want):
            out.append(f"Medic's {what} limit is {got:g} {unit}, not {want:g} {unit}")
    if end["oom_killed"]:
        out.append("Medic was killed for memory (OOMKilled)")
    if end["restarts"] > start["restarts"]:
        out.append(f"Medic restarted {end['restarts'] - start['restarts']} time(s) during the run")
    return out


def percentile(values: list[float], pct: float = 0.95) -> float:
    """Linear percentile (numpy's default)."""
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct
    low, high = math.floor(rank), math.ceil(rank)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def store_kib(stack) -> int | None:
    try:
        return int(stack.medic_exec("du", "-sk", STORE_PATH).split()[0])
    except (IndexError, ValueError):
        return None


def summarize(samples: list[dict], services: tuple[str, ...] = SERVICES) -> dict:
    """Per service: p95 CPU and peak memory, with n."""
    out = {}
    for svc in services:
        rows = [s[svc] for s in samples if svc in s]
        if rows:
            out[svc] = {
                "n": len(rows),
                "cpu_p95_vcpu": round(percentile([r[0] for r in rows]), 4),
                "cpu_max_vcpu": round(max(r[0] for r in rows), 4),
                "rss_peak_mib": round(max(r[1] for r in rows), 1),
            }
        else:
            out[svc] = {"n": 0}
    return out


class Sampler:
    """`stack.usage()` in a background thread."""

    def __init__(self, stack, interval_s: float = 60.0) -> None:
        self.stack, self.interval_s = stack, interval_s
        self.samples: list[dict] = []  # {"at": epoch, <service>: (vCPU, MiB)}
        self.failures = 0
        self.limits: dict = {"start": None, "end": None}  # stack.medic_limits()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def take(self) -> dict | None:
        rows = self.stack.usage()
        return {"at": time.time(), **rows} if rows else None

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                sample = self.take()
            except Exception:  # noqa: BLE001 - one bad read must not end the sampling
                sample = None
            if sample and len(sample) > 1:
                self.samples.append(sample)
            else:
                self.failures += 1
            self._stop.wait(max(self.interval_s - (time.monotonic() - started), 0.5))

    def _limits(self, at: str) -> None:
        try:
            self.limits[at] = self.stack.medic_limits()
        except Exception:  # noqa: BLE001 - an unread limit is a floor missed, not a crash
            self.limits[at] = None

    def floor(self) -> list[str]:
        return floor_failures(self.limits["start"], self.limits["end"])

    def start(self) -> None:
        self._limits("start")  # the restart baseline
        self._thread.start()

    def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=60)
        self._limits("end")  # at the end, so a limit changed mid-run is caught
