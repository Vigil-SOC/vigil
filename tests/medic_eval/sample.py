"""Sample Medic's containers from outside the process.

`stack.usage()` (`docker stats --no-stream` on Compose's medic, medic-gateway and
medic-dockerproxy; `crictl stats` on kind's medic and medic-gateway), once an
interval, for as long as the sampler runs; `du -sk` of the store at the start and
the end. The advisory bars are constants the artifact carries; only the hard
limits (HARD_*) turn a run red.
"""

from __future__ import annotations

import math
import re
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
# The only limits that fail the run.
HARD_CPU_VCPU = 1.0
HARD_RSS_MIB = 1024.0
# Medic is one Python process, so a flood pins it at 1.00 vCPU; docker's own reading
# wobbles by about a percent around that. Over by more than this is over.
CPU_TOLERANCE = 0.02

# `docker stats` mixes IEC (KiB/MiB/GiB) and SI (kB/MB/GB).
_UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "kib": 1024, "mib": 1024**2, "gib": 1024**3}
_SIZE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([A-Za-z]*)\s*$")
FORMAT = "{{.ID}}\t{{.CPUPerc}}\t{{.MemUsage}}"  # id first: the name carries the project


def parse_bytes(text: str) -> float:
    m = _SIZE.match(text)
    if not m:
        raise ValueError(f"unparsed size {text!r}")
    return float(m[1]) * _UNITS[(m[2] or "b").lower()]


def parse_stats(text: str) -> dict[str, tuple[float, float]]:
    """{container id prefix: (vCPU, MiB)} from `docker stats --format FORMAT`."""
    rows = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        cid, cpu, mem = line.split("\t")[:3]
        rows[cid] = (float(cpu.strip().rstrip("%")) / 100, parse_bytes(mem.split("/")[0]) / 1024**2)
    return rows


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

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=60)
