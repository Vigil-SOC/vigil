"""The flood phase: Medic's log streams over capacity, from outside.

Throwaway containers carrying the eval project's own Compose labels for three Vigil
services log RATE lines/s each for `seconds`; the Docker proxy lists them like the
real ones, so Medic's log tail reads them. Own phase: it overlaps no scored cell.

The floors: Medic stays under 1 GiB and keeps writing (the heartbeat cycle advances
and its store keeps recording; Medic writes decisions on events, not on a clock, so
the seq is reported and fails only if it goes backwards); and it keeps up: by the end
of the settle window its counts have stopped moving, and every flooded line it could
not admit is counted in `dropped_last_hour` (#3478). The API shows no per-sensor
`degraded` (that is the sensor_health observation's state, which is not served), so
`dropped_last_hour` is what it can read. CPU is reported against the 0.5 advisory bar,
never red: the container's 1 vCPU quota enforces the limit, so a reading over it is
`docker stats` wobble. Everything else is reported.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from tests.medic_eval import medic_api
from tests.medic_eval.sample import HARD_RSS_MIB, Sampler, summarize

SERVICES = ("backend", "soc-daemon", "llm-worker")  # three of the seven log streams
RATE = 5000  # lines/s per stream
DURATION_S = 60
SETTLE_S = 120  # for Medic's tail to catch up and its sensors to report
# Quiet before the flood: LogStream lists containers each 30 s (logtail REDISCOVER_S
# caps it at one list per 10 s), so by then Medic follows the flood containers. Without
# it the first ~30 s of the flood is a backlog Medic reads flat out on attach, and the
# phase measures that catch-up rather than a flood on a stream it already follows.
LEAD_S = 40
STABLE_S = 35  # dropped counts unchanged this long (past one 30 s report): settled

# Lines in core/telemetry.py's JSON shape (logtail reads them as python_json): of each
# ten, one is a catalogue template (counted by template over the rate), two are WARNING
# and seven are INFO under a template the pack doesn't know (by level / dropped).
SCRIPT = r"""
import sys, time
svc, seconds, rate, lead = sys.argv[1], float(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4])
print(f"FLOOD-READY {svc}", flush=True)
time.sleep(lead)
ts, tr = "2026-01-01T00:00:00.000000+00:00", "0" * 32
CAT = '{"ts": "%s", "level": "INFO", "logger": "services.daemon.processor", "message": "Database service initialized", "msg_template": "Database service initialized", "trace_id": "%s", "span_id": ""}\n' % (ts, tr)
OWN = '{"ts": "%s", "level": "%s", "logger": "flood.%s", "message": "flood %s line %d padding padding", "msg_template": "flood %%s line %%d padding padding", "trace_id": "%s", "span_id": ""}\n'
def line(k):
    if k % 10 == 0:
        return CAT
    return OWN % (ts, "WARNING" if k % 10 < 3 else "INFO", svc, svc, k, tr)
per, tick, n = rate // 10, 0.1, 0
end = time.monotonic() + seconds
while time.monotonic() < end:
    t = time.monotonic()
    sys.stdout.write("".join(line(n + i) for i in range(per)))
    sys.stdout.flush()
    n += per
    time.sleep(max(0.0, tick - (time.monotonic() - t)))
print(f"FLOOD-DONE {svc} {n}", flush=True)
"""
LOGTAIL = Path(__file__).resolve().parents[2] / "services" / "medic" / "sensors" / "logtail.py"


def admit_stream_per_s() -> int:
    """logtail's per-stream admit rate, read from its source: the nightly's Python has no httpx."""
    return int(re.search(r"^ADMIT_STREAM_PER_S = ([\d_]+)", LOGTAIL.read_text(), re.M)[1])


def _run(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _sensor_ids(sensors: dict, service: str) -> list[str]:
    """The log sensor ids for a service (`...logs.<service>` or `log_<service>`)."""
    key = service.replace("-", "_")
    return [i for i in sensors if i.endswith(("." + service, "." + key)) and "log" in i]


def run_flood(stack, seconds: int = DURATION_S, rate: int = RATE) -> dict:
    """Run the flood and return what it showed, with `failures` for each floor missed."""
    before, sensors_before = medic_api.status(stack), medic_api.sensors(stack)
    names = {s: f"{stack.project}-flood-{s}" for s in SERVICES}
    image = f"{stack.project}-backend:local"
    sampler = Sampler(stack, interval_s=5)
    started = time.time()
    lead_end = time.monotonic() + LEAD_S  # from the first container's start
    try:
        for svc, name in names.items():
            _run("docker", "rm", "-f", name)
            done = _run(
                "docker", "run", "-d", "--name", name, "--network", "none",
                "--label", f"com.docker.compose.project={stack.project}",
                "--label", f"com.docker.compose.service={svc}",
                "--entrypoint", "python", image, "-c", SCRIPT, svc, str(seconds), str(rate), str(LEAD_S),
            )
            if done.returncode:
                raise AssertionError(f"flood container {name}: {done.stderr[-500:]}")
        time.sleep(max(0.0, lead_end - time.monotonic()))
        sampler.start()  # the flood and 5 s past it: Medic is behind only if pinned in it
        time.sleep(seconds + 5)
        sampler.stop()
        # Medic's tail reads a stream at its own pace: settle until each flooded sensor
        # has counted and its count has stopped moving.
        flooded = {s: _sensor_ids(medic_api.sensors(stack), s) for s in SERVICES}
        settle_start = time.monotonic()
        deadline, last, moved, settled = settle_start + SETTLE_S, None, settle_start, "deadline"
        while time.monotonic() < deadline:
            now = medic_api.sensors(stack)
            counts = [now.get(i, {}).get("dropped_last_hour") or 0 for ids in flooded.values() for i in ids]
            if counts != last:
                last, moved = counts, time.monotonic()
            elif all(any((now.get(i, {}).get("dropped_last_hour") or 0) > 0 for i in ids) for ids in flooded.values() if ids) \
                    and time.monotonic() - moved >= STABLE_S:
                settled = "stable"
                break
            time.sleep(5)
        settle_s = round(time.monotonic() - settle_start)
        after, sensors_after = medic_api.status(stack), medic_api.sensors(stack)
        emitted = {
            s: _run("docker", "logs", "--tail", "1", n).stdout.strip() for s, n in names.items()
        }
    finally:
        sampler.stop()
        for name in names.values():
            _run("docker", "rm", "-f", name)

    streams = {}
    for svc in SERVICES:
        rows = [sensors_after[i] for i in _sensor_ids(sensors_after, svc)]
        streams[svc] = [
            {"sensor": r["id"], "state": r["state"], "dropped_last_hour": r.get("dropped_last_hour")}
            for r in rows
        ]
    # Keep-up: every line over the most the rate rule could admit in the flood's
    # seconds is counted. The real service's container shares the sensor, so a count
    # can only come out high.
    allowance = admit_stream_per_s() * seconds
    keepup = {}
    for svc in SERVICES:
        done = re.fullmatch(rf"FLOOD-DONE {re.escape(svc)} (\d+)", emitted.get(svc, ""))
        sent = int(done[1]) if done else None
        dropped = sum(r["dropped_last_hour"] or 0 for r in streams[svc])
        short = None if sent is None else max(0, sent - allowance - dropped)
        keepup[svc] = {"sent": sent, "admit_allowance": allowance, "dropped_last_hour": dropped, "shortfall": short}
    usage = summarize(sampler.samples)
    cycle = lambda st: st["cycle"] if st else None  # noqa: E731
    seq = lambda st: ((st or {}).get("chain_head") or {}).get("seq")  # noqa: E731
    failures = []
    for svc, u in usage.items():
        if u["n"] and u["rss_peak_mib"] > HARD_RSS_MIB:
            failures.append(f"{svc} peak memory {u['rss_peak_mib']} MiB over {HARD_RSS_MIB:.0f}")
    if not usage["medic"]["n"]:
        failures.append("no docker stats samples of Medic during the flood")
    if not (before and after):
        failures.append("Medic's status could not be read around the flood")
    else:
        if cycle(after) <= cycle(before):
            failures.append(f"heartbeat cycle did not advance ({cycle(before)} -> {cycle(after)})")
        if (seq(after) or -1) < (seq(before) or -1):
            failures.append(f"decision seq went backwards ({seq(before)} -> {seq(after)})")
        if after["store"]["states"] != ["ok"] or after["store"]["decisions_lost"] > before["store"]["decisions_lost"]:
            failures.append(f"Medic's store is not recording ({after['store']['states']}, lost {after['store']['decisions_lost']})")
    for svc, rows in streams.items():
        if not rows:
            failures.append(f"no log sensor found for {svc}")
        elif not any((r["dropped_last_hour"] or 0) > 0 for r in rows):
            failures.append(f"{svc}: no dropped lines counted")
        k = keepup[svc]
        if k["sent"] is None:
            failures.append(f"{svc}: no FLOOD-DONE line, so the lines sent are unknown ({emitted.get(svc)!r})")
        elif k["shortfall"]:
            failures.append(
                f"{svc}: Medic fell behind, {k['shortfall']} lines neither admitted nor counted "
                f"(sent {k['sent']}, admit allowance {allowance}, dropped_last_hour {k['dropped_last_hour']})"
            )
    if settled == "deadline":
        failures.append(f"the dropped counts had not settled (still moving, or a stream never counted) in {SETTLE_S} s")
    return {
        "lines_per_s_per_stream": rate,
        "seconds": seconds,
        "emitted": emitted,
        "started_at": started,
        "usage": usage,
        "stats_samples": len(sampler.samples),
        "cycle": [cycle(before), cycle(after)],
        "decision_seq": [seq(before), seq(after)],
        "medic_state": [(before or {}).get("state"), (after or {}).get("state")],
        "streams": streams,
        "settle": {"exit": settled, "seconds": settle_s, "limit_s": SETTLE_S},
        "keepup": keepup,
        "failures": failures,
    }
