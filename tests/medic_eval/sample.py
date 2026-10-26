"""Sample Medic from outside the process (C7-limits).

One stdlib script, three backends, no metrics stack:

  compose   ``docker stats --no-stream`` on the three Compose containers
  helm      ``kubectl top pod`` for Medic's own pods
  host      ``ps`` (CPU is the cputimes delta over the sample window, not lifetime %cpu)

Prints JSON per component: CPU p95 (vCPU, 100% == 1), peak RSS (MiB), and an
RSS least-squares slope (MiB/day). Store growth (MiB/day) is ``du -sk`` inside
the Medic container or pod, or a host directory (``--store``). The default
interval is 60 s; ``--interval`` and ``--duration`` are seconds.

``within_budget`` compares those numbers to ``BUDGET`` and does not change the
exit code. The nightly run owns the gate.

    python -m tests.medic_eval.sample --backend compose --interval 60 --duration 300
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Epic pass bar. Readers (and the nightly gate) import this rather than copy it.
BUDGET = {
    "cpu_p95_vcpu": 0.5,
    "peak_rss_mib": 512.0,
    "rss_slope_mib_per_day": 5.0,
    "store_growth_mib_per_day": 12.0,
}

COMPOSE_CONTAINERS = (
    "deeptempo-medic",
    "deeptempo-medic-gateway",
    "deeptempo-medic-dockerproxy",
)
STORE_PATH = "/var/lib/vigil-medic"
DEFAULT_INTERVAL = 60.0

# Name, CPU%, memory usage / limit. Tab-separated so names stay one field.
DOCKER_STATS_FORMAT = "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"

_DAY = 86400.0
_MIB = 1024.0 * 1024.0

# Docker stats mixes IEC (KiB/MiB/GiB) and SI (kB/MB/GB). kubectl top uses Ki/Mi/Gi.
_UNITS = {
    "": 1.0,
    "b": 1.0,
    "k": 1000.0,
    "kb": 1000.0,
    "m": 1000.0**2,
    "mb": 1000.0**2,
    "g": 1000.0**3,
    "gb": 1000.0**3,
    "t": 1000.0**4,
    "tb": 1000.0**4,
    "ki": 1024.0,
    "kib": 1024.0,
    "mi": 1024.0**2,
    "mib": 1024.0**2,
    "gi": 1024.0**3,
    "gib": 1024.0**3,
    "ti": 1024.0**4,
    "tib": 1024.0**4,
}

_SIZE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([A-Za-z]*)\s*$")
# Default `docker stats` table: optional 12-hex id, name, cpu%, usage before the slash.
_TABLE = re.compile(
    r"^(?:[0-9a-f]{12}\s+)?(?P<name>\S+)\s+"
    r"(?P<cpu>[0-9]+(?:\.[0-9]+)?%)\s+"
    r"(?P<mem>\S+)\s*/"
)

Reading = tuple[float, float]  # cpu vCPU, rss MiB


def parse_bytes(text: str) -> float:
    """Bytes from a docker or kubectl size token (``512KiB``, ``1.5MB``, ``128Mi``)."""
    match = _SIZE.match(text.strip())
    if not match:
        raise ValueError(f"unparsed size {text!r}")
    number, unit = match.group(1), match.group(2).lower()
    try:
        scale = _UNITS[unit]
    except KeyError:
        raise ValueError(f"unknown size unit {unit!r} in {text!r}") from None
    return float(number) * scale


def bytes_to_mib(nbytes: float) -> float:
    return nbytes / _MIB


def parse_cpu_percent(text: str) -> float:
    """``100%`` is 1 vCPU. ``ps`` prints the same percentage without a sign."""
    return float(text.strip().removesuffix("%")) / 100.0


def parse_cpu_quantity(text: str) -> float:
    """kubectl CPU: ``15m`` is 0.015 vCPU, ``2`` is 2, ``100n`` is 1e-7."""
    token = text.strip()
    if token.endswith("%"):
        return parse_cpu_percent(token)
    if token.endswith("n"):
        return float(token[:-1]) / 1e9
    if token.endswith("u"):
        return float(token[:-1]) / 1e6
    if len(token) > 1 and token.endswith("m"):
        return float(token[:-1]) / 1000.0
    return float(token)


def parse_docker_stats(text: str) -> list[tuple[str, float, float]]:
    """Rows of (name, vCPU, rss MiB) from a formatted or default ``docker stats``."""
    rows: list[tuple[str, float, float]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("CONTAINER") or "CPU %" in line:
            continue
        if "\t" in line:
            parts = line.split("\t")
            if len(parts) < 3:
                raise ValueError(f"short docker stats line: {line!r}")
            name, cpu, mem = parts[0].strip(), parts[1], parts[2]
        else:
            match = _TABLE.search(line)
            if not match:
                raise ValueError(f"unparsed docker stats line: {line!r}")
            name = match.group("name")
            cpu = match.group("cpu")
            mem = match.group("mem")
        usage = mem.split("/")[0].strip()
        if usage == "--":
            raise ValueError(f"no memory usage for {name}")
        rows.append((name, parse_cpu_percent(cpu), bytes_to_mib(parse_bytes(usage))))
    if not rows:
        raise ValueError("docker stats produced no rows")
    return rows


def parse_kubectl_top(text: str) -> list[tuple[str, float, float]]:
    rows: list[tuple[str, float, float]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("NAME") or "CPU(cores)" in line:
            continue
        parts = line.split()
        if len(parts) < 3:
            raise ValueError(f"short kubectl top line: {line!r}")
        name, cpu, mem = parts[0], parts[1], parts[2]
        rows.append((name, parse_cpu_quantity(cpu), bytes_to_mib(parse_bytes(mem))))
    if not rows:
        raise ValueError("kubectl top produced no rows")
    return rows


def parse_ps(text: str) -> dict[int, tuple[float, float, float]]:
    """``ps -o pid=,cputimes=,rss=,pcpu=``.

    ``cputimes`` is cumulative CPU seconds (whole seconds). ``rss`` is KiB.
    ``pcpu`` is lifetime CPU, not the sample window — callers turn ``cputimes``
    into an interval rate.
    """
    found: dict[int, tuple[float, float, float]] = {}
    for raw in text.splitlines():
        parts = raw.split()
        if len(parts) < 4:
            continue
        pid = int(parts[0])
        found[pid] = (
            float(parts[1]),
            bytes_to_mib(float(parts[2]) * 1024.0),
            parse_cpu_percent(parts[3]),
        )
    return found


def parse_du_kib(text: str) -> int:
    """First field of ``du -sk`` is 1024-byte blocks."""
    line = text.strip().splitlines()
    if not line:
        raise ValueError("du produced no output")
    return int(line[0].split()[0]) * 1024


def percentile(values: list[float], pct: float = 0.95) -> float:
    """Linear percentile (numpy's default). One sample returns that sample."""
    if not values:
        raise ValueError("no samples")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def slope_per_day(times_s: list[float], values: list[float]) -> float:
    """Least-squares slope of ``values`` against ``times_s``, in units per day.

    Fewer than two samples, or no elapsed time, is 0 (nothing to fit).
    """
    if len(times_s) != len(values):
        raise ValueError("times and values differ in length")
    count = len(times_s)
    if count < 2:
        return 0.0
    mean_t = sum(times_s) / count
    mean_y = sum(values) / count
    var_t = sum((t - mean_t) ** 2 for t in times_s)
    if var_t == 0.0:
        return 0.0
    cov = sum((t - mean_t) * (y - mean_y) for t, y in zip(times_s, values))
    return (cov / var_t) * _DAY


def directory_bytes(path: Path) -> int:
    root = path.resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            try:
                total += (Path(dirpath) / name).lstat().st_size
            except OSError:
                continue
    return total


def _run(argv: list[str]) -> str:
    proc = subprocess.run(argv, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"{argv[0]} failed ({proc.returncode}): {detail}")
    return proc.stdout


def _index(
    rows: list[tuple[str, float, float]], wanted: list[str]
) -> dict[str, Reading]:
    found = {name: (cpu, rss) for name, cpu, rss in rows}
    missing = [name for name in wanted if name not in found]
    if missing:
        raise RuntimeError(f"missing stats for {', '.join(missing)}")
    return {name: found[name] for name in wanted}


def _select_pods(
    rows: list[tuple[str, float, float]], matches: list[str]
) -> dict[str, Reading]:
    chosen: dict[str, Reading] = {}
    for name, cpu, rss in rows:
        if not matches or any(token in name for token in matches):
            chosen[name] = (cpu, rss)
    if not chosen:
        raise RuntimeError("kubectl top returned no matching pods")
    return chosen


def _component(
    name: str, cpus: list[float], rss: list[float], times: list[float]
) -> dict:
    cpu_p95 = percentile(cpus)
    peak = max(rss)
    slope = slope_per_day(times, rss)
    within = (
        cpu_p95 <= BUDGET["cpu_p95_vcpu"]
        and peak <= BUDGET["peak_rss_mib"]
        and slope <= BUDGET["rss_slope_mib_per_day"]
    )
    return {
        "name": name,
        "cpu_p95_vcpu": cpu_p95,
        "peak_rss_mib": peak,
        "rss_slope_mib_per_day": slope,
        "within_budget": within,
    }


def run_sample(
    backend: str,
    *,
    interval: float = DEFAULT_INTERVAL,
    duration: float = DEFAULT_INTERVAL,
    targets: list[str] | None = None,
    store: Path | None = None,
    store_path: str = STORE_PATH,
    store_container: str | None = None,
    store_pod: str | None = None,
    measure_store: bool = True,
    run=None,
    monotonic=None,
    sleep=None,
) -> dict:
    """Sample until ``duration`` seconds. Times are monotonic seconds at each take.

    ``run`` is ``argv -> stdout`` (tests pass a stub). ``sleep`` and ``monotonic``
    default to the real clock; a test advances them so a slope is exact.
    """
    if interval <= 0:
        raise ValueError("interval must be > 0")
    if duration < 0:
        raise ValueError("duration must be >= 0")
    run = run or _run
    monotonic = monotonic or time.monotonic
    sleep = sleep or time.sleep
    names, take = _backend(
        backend,
        targets=list(targets or []),
        store=store,
        store_path=store_path,
        store_container=store_container,
        store_pod=store_pod,
        measure_store=measure_store,
        run=run,
        monotonic=monotonic,
    )

    times: list[float] = []
    series: list[dict[str, Reading]] = []
    store_samples: list[int] = []
    origin = monotonic()
    while True:
        start = monotonic()
        now = start - origin
        times.append(now)
        readings, store_bytes = take()
        series.append(readings)
        if store_bytes is not None:
            store_samples.append(store_bytes)
        if now >= duration:
            break
        # `docker stats` takes a moment; sleep the rest of the interval. When
        # duration is not a multiple of it, the last sample lands on duration.
        wait = min(interval, duration - now) - (monotonic() - start)
        if wait > 0:
            sleep(wait)

    order = _component_order(names, series)
    components = []
    for name in order:
        present = [(t, row[name]) for t, row in zip(times, series) if name in row]
        if not present:
            continue
        ts = [t for t, _reading in present]
        cpus = [reading[0] for _t, reading in present]
        rss = [reading[1] for _t, reading in present]
        # Host sample 0 is lifetime %cpu (no prior cputimes). Later points are
        # the interval rate, which is what the bar measures.
        if backend == "host" and len(cpus) > 1:
            cpus = cpus[1:]
        components.append(_component(name, cpus, rss, ts))

    store_report = None
    if store_samples:
        growth = slope_per_day(times, [n / _MIB for n in store_samples])
        store_report = {
            "path": str(store) if store is not None else store_path,
            "growth_mib_per_day": growth,
            "within_budget": growth <= BUDGET["store_growth_mib_per_day"],
        }
    within = all(item["within_budget"] for item in components) and (
        store_report is None or store_report["within_budget"]
    )
    return {
        "backend": backend,
        "interval_s": interval,
        "duration_s": duration,
        "sample_count": len(series),
        "elapsed_s": times[-1],
        "budget": dict(BUDGET),
        "within_budget": within,
        "store": store_report,
        "components": components,
    }


def _backend(
    backend: str,
    *,
    targets: list[str],
    store: Path | None,
    store_path: str,
    store_container: str | None,
    store_pod: str | None,
    measure_store: bool,
    run,
    monotonic,
):
    if backend == "compose":
        return _compose(targets, store_path, store_container, measure_store, run)
    if backend == "helm":
        return _helm(targets, store_path, store_pod, measure_store, run)
    if backend == "host":
        return _host(targets, store, measure_store, run, monotonic)
    raise ValueError(f"unknown backend {backend!r}")


def _compose(targets, store_path, store_container, measure_store, run):
    containers = targets or list(COMPOSE_CONTAINERS)
    container = store_container
    if container is None:
        container = (
            "deeptempo-medic" if "deeptempo-medic" in containers else containers[0]
        )

    def take():
        text = run(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                DOCKER_STATS_FORMAT,
                *containers,
            ]
        )
        rows = _index(parse_docker_stats(text), containers)
        store_bytes = None
        if measure_store:
            store_bytes = parse_du_kib(
                run(["docker", "exec", container, "du", "-sk", store_path])
            )
        return rows, store_bytes

    return containers, take


# Chart label on Medic and its gateway only (values medic, medic-gateway).
# `kubectl top pod` with no selector would sample the whole namespace.
_HELM_SELECTOR = "app.kubernetes.io/component in (medic,medic-gateway)"


def _helm(targets, store_path, store_pod, measure_store, run):
    def take():
        text = run(["kubectl", "top", "pod", "--no-headers", "-l", _HELM_SELECTOR])
        rows = _select_pods(parse_kubectl_top(text), targets)
        names = list(rows)
        pod = store_pod
        if measure_store and pod is None:
            pod = _medic_pod(names)
        store_bytes = None
        if measure_store:
            if pod is None:
                raise RuntimeError(
                    "pass --store-pod (no single Medic pod in kubectl top)"
                )
            store_bytes = parse_du_kib(
                run(["kubectl", "exec", pod, "--", "du", "-sk", store_path])
            )
        return rows, store_bytes

    # Pod set can change between takes; the report unions the names.
    return [], take


def _medic_pod(names: list[str]) -> str | None:
    candidates = [
        name
        for name in names
        if "medic" in name and "gateway" not in name and "dockerproxy" not in name
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _component_order(names: list[str], series: list[dict[str, Reading]]) -> list[str]:
    seen: list[str] = []
    known: set[str] = set()
    for row in series:
        for name in row:
            if name not in known:
                known.add(name)
                seen.append(name)
    if names and all(name in known for name in names):
        return list(names) + [name for name in seen if name not in names]
    return seen


def _host(targets, store, measure_store, run, monotonic):
    pids: dict[str, int] = {}
    for target in targets:
        if "=" not in target:
            raise ValueError(f"host target must be name=pid, got {target!r}")
        name, pid = target.split("=", 1)
        pids[name] = int(pid)
    if not pids:
        raise ValueError("host backend needs at least one --target name=pid")
    if measure_store and store is None:
        raise ValueError("host backend needs --store or --no-store")
    # pid -> (cumulative CPU seconds, wall time). %cpu is the process lifetime,
    # so a long-running Medic would report its average since start. The rate
    # since the previous sample is the same window docker stats and kubectl top use.
    previous: dict[int, tuple[float, float]] = {}

    def take():
        text = run(
            [
                "ps",
                "-o",
                "pid=,cputimes=,rss=,pcpu=",
                "-p",
                ",".join(str(pid) for pid in pids.values()),
            ]
        )
        wall = monotonic()
        by_pid = parse_ps(text)
        rows: dict[str, Reading] = {}
        for name, pid in pids.items():
            try:
                cpu_s, rss, lifetime = by_pid[pid]
            except KeyError:
                raise RuntimeError(f"ps did not report pid {pid} ({name})") from None
            prior = previous.get(pid)
            if prior is None:
                vcpu = lifetime
            else:
                elapsed = wall - prior[1]
                vcpu = (cpu_s - prior[0]) / elapsed if elapsed > 0 else 0.0
                vcpu = max(0.0, vcpu)
            previous[pid] = (cpu_s, wall)
            rows[name] = (vcpu, rss)
        store_bytes = directory_bytes(store) if measure_store else None
        return rows, store_bytes

    return list(pids), take


def _positive(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return value


def _non_negative(text: str) -> float:
    value = float(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sample Medic from outside the process."
    )
    parser.add_argument(
        "--backend", choices=("compose", "helm", "host"), default="compose"
    )
    parser.add_argument("--interval", type=_positive, default=DEFAULT_INTERVAL)
    parser.add_argument("--duration", type=_non_negative, default=DEFAULT_INTERVAL)
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help="compose: container. helm: pod-name substring. host: name=pid. Repeatable.",
    )
    parser.add_argument(
        "--store", type=Path, default=None, help="Host directory to measure."
    )
    parser.add_argument(
        "--store-path", default=STORE_PATH, help="Path inside the container or pod."
    )
    parser.add_argument("--store-container", default=None)
    parser.add_argument("--store-pod", default=None)
    parser.add_argument("--no-store", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run_sample(
            args.backend,
            interval=args.interval,
            duration=args.duration,
            targets=args.target,
            store=args.store,
            store_path=args.store_path,
            store_container=args.store_container,
            store_pod=args.store_pod,
            measure_store=not args.no_store,
        )
    except (RuntimeError, ValueError, FileNotFoundError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
