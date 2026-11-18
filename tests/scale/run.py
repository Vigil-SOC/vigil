"""The weekly scale run: four limits of one Vigil install, measured, not targeted.

    python -m tests.scale.run --out scale-out

Brings up the stack (stack.py), then measures:

1. Open findings with the console still responsive. Findings are imported up to
   each count in FINDING_STEPS, and at each the two reads the Findings page makes on
   load (the list's first page and the summary) are timed, with a filtered, a deep
   and a searched page beside them. The limit is the largest count at which both
   page-load reads keep their p95 within RESPONSIVE_S.
2. Open cases at once. The same for the case queue's first page, at CASE_STEPS.
3. /api/v1 requests per second. A mix of reads (api_mix) from 1, 4, 16 and 64
   callers at once. The limit is the most requests per second any of those held
   with p95 within RESPONSIVE_S and under 1% of requests failing.
4. Alerts per minute at intake. INTAKE_SENDERS push alerts to the daemon's /ingest
   webhook back to back for INTAKE_SECONDS, which is more than it stores: its queue
   fills and it holds the senders. The limit is the rate rows are stored at while
   that lasts, after INTAKE_LEAD_S. Each stored alert is looked up against the open
   cases and offered for investigation, as on an install; nothing takes the offer up.

3 and 4 depend on how much is stored, so they are always taken at the same point:
REFERENCE_FINDINGS findings and REFERENCE_CASES cases, which every run reaches. The
counts above those come afterwards and share a time budget; a count whose import
would not fit in what is left of it is reported as not reached, and changes no
other number.

The numbers are for the machine the run is on (the report names its CPUs and
memory) with every service on that one host, load generator included. A shared CI
runner varies from week to week, so read them as a trend; nothing here fails on a
number. The run fails only when the stack does not come up or a step cannot be
measured.
"""

from __future__ import annotations

import argparse
import http.client
import itertools
import json
import math
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.scale.stack import REPO, Stack, StackFailed

FINDING_STEPS = (10_000, 50_000, 100_000, 250_000, 1_000_000)
CASE_STEPS = (1_000, 5_000, 20_000)
REFERENCE_FINDINGS = 100_000  # stored when the API and intake are measured
REFERENCE_CASES = 5_000
API_CALLERS = (1, 4, 16, 64)
RESPONSIVE_S = 1.0  # a page-load read slower than this at p95 is not responsive
SAMPLES = 40  # timed requests per read, after WARMUP untimed ones
WARMUP = 2
READ_BUDGET_S = 60  # a read that has taken this long in all is sampled no further
API_SECONDS = 30
INTAKE_SECONDS = 90
INTAKE_LEAD_S = 15  # not counted: the queue is filling
INTAKE_BATCH = 10  # alerts per push
INTAKE_SENDERS = 8
IMPORT_CHUNK = 25_000  # findings per uploaded file
SEVERITIES = ("low", "medium", "high", "critical")
TECHNIQUES = ("T1059", "T1078", "T1021", "T1486", "T1566", "T1071", "T1003", "T1110")


def alert(prefix: str, i: int, now: datetime, block: int = 0) -> dict:
    """One synthetic alert, about 600 bytes, distinct by id and spread over 30 days.

    ``block`` moves its source address out of the imported findings' range, so an
    alert at intake shares an address with no open case and is attached to none.
    """
    return {
        "finding_id": f"{prefix}-{i:07d}",
        "data_source": "scale",
        "timestamp": (now - timedelta(seconds=(i * 37) % (30 * 86400))).isoformat(),
        "severity": SEVERITIES[i % 4],
        "anomaly_score": ((i * 7919) % 1000) / 1000,
        "title": f"Unusual outbound connection from host-{i % 5000:04d}",
        "description": (
            f"host-{i % 5000:04d} opened {20 + i % 300} connections to an address "
            f"it has not contacted before, as user{i % 2000:04d}, outside its usual hours."
        ),
        "entity_context": {
            "src_ip": f"10.{block + ((i >> 16) & 127)}.{(i >> 8) & 255}.{i & 255}",
            "dst_ip": f"203.0.{(i >> 8) & 255}.{i & 255}",
            "hostname": f"host-{i % 5000:04d}",
            "user": f"user{i % 2000:04d}",
        },
        "mitre_predictions": {
            TECHNIQUES[i % 8]: 0.5 + (i % 50) / 100,
            TECHNIQUES[(i + 3) % 8]: 0.2,
        },
    }


def case(i: int) -> dict:
    return {
        "case_id": f"scale-case-{i:06d}",
        "title": f"Outbound connections from host-{i % 5000:04d}",
        "description": "Imported by the scale run.",
        "status": "new",
        "priority": ("low", "medium", "high", "critical")[i % 4],
        "finding_ids": [f"scale-{i:07d}"],
        "tags": ["scale"],
    }


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


class Caller:
    """One connection to one port, reopened if the server closes it."""

    def __init__(self, port: int, headers: dict[str, str]) -> None:
        self.port, self.headers, self.conn = port, headers, None

    def request(
        self, method: str, path: str, body: bytes | None = None,
        content_type: str = "application/json", timeout: float = 120.0,
    ) -> tuple[int, bytes, float]:
        """(status, body, seconds). Status 0 is a request that got no response."""
        headers = dict(self.headers)
        if body is not None:
            headers["Content-Type"] = content_type
        started = time.perf_counter()
        while True:
            reused = self.conn is not None
            try:
                if self.conn is None:
                    self.conn = http.client.HTTPConnection(
                        "127.0.0.1", self.port, timeout=timeout
                    )
                self.conn.request(method, path, body, headers)
                resp = self.conn.getresponse()
                data = resp.read()
                return resp.status, data, time.perf_counter() - started
            except (OSError, http.client.HTTPException) as exc:
                self.close()
                # A kept connection the server closed while it sat idle: once more
                # on a new one. A timeout is the request's own failure.
                if reused and not isinstance(exc, TimeoutError):
                    continue
                return 0, str(exc).encode(), time.perf_counter() - started

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None


def timed_read(caller: Caller, path: str) -> dict:
    """p50 and p95 of SAMPLES requests made one after another."""
    seconds, failed, began = [], 0, time.monotonic()
    for n in range(WARMUP + SAMPLES):
        if len(seconds) >= 3 and time.monotonic() - began > READ_BUDGET_S:
            break
        status, body, took = caller.request("GET", path)
        if n < WARMUP:
            continue
        if status != 200:
            failed += 1
            detail = body[:200].decode("utf-8", "replace")
            print(f"  GET {path}: HTTP {status} {detail}", flush=True)
        seconds.append(took)
    return {
        "path": path,
        "p50_s": round(percentile(seconds, 50), 4),
        "p95_s": round(percentile(seconds, 95), 4),
        "failed": failed,
    }


# ---- storing what the reads are measured against -----------------------------


def upload(caller: Caller, name: str, payload: bytes) -> dict:
    """One file through the console's import, waited to its end. The job's stats."""
    boundary = "scale" + os.urandom(8).hex()
    body = b"".join((
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"data_type\"\r\n\r\n"
        f"finding\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{name}\"\r\nContent-Type: application/json\r\n\r\n".encode(),
        payload,
        f"\r\n--{boundary}--\r\n".encode(),
    ))
    status, data, _ = caller.request(
        "POST", "/api/ingest/upload", body,
        content_type=f"multipart/form-data; boundary={boundary}", timeout=600,
    )
    if status != 202:
        raise StackFailed(f"import of {name}: HTTP {status} {data[:300]!r}")
    job = json.loads(data)
    while job["status"] == "running":
        time.sleep(0.5)
        status, data, _ = caller.request("GET", f"/api/ingest/jobs/{job['job_id']}")
        if status == 404:
            # Jobs are held by the API process that took the upload, and the image
            # runs two. A new connection may reach the other; ask again.
            caller.close()
            continue
        if status != 200:
            raise StackFailed(f"import job {job['job_id']}: HTTP {status} {data[:300]!r}")
        job = json.loads(data)
    if job["status"] != "succeeded":
        raise StackFailed(f"import of {name} ended {job['status']}: {job.get('error')}")
    return job["stats"]


def import_findings(caller: Caller, start: int, end: int, now: datetime) -> float:
    """Findings scale-<start> to scale-<end - 1>, by file import. Rows a second."""
    began = time.perf_counter()
    for at in range(start, end, IMPORT_CHUNK):
        upto = min(at + IMPORT_CHUNK, end)
        lines = "\n".join(json.dumps(alert("scale", i, now)) for i in range(at, upto))
        stats = upload(caller, f"scale-{at}.jsonl", lines.encode())
        if stats.get("findings_imported") != upto - at:
            raise StackFailed(f"import of findings {at} to {upto}: {stats}")
    return (end - start) / (time.perf_counter() - began)


def import_cases(caller: Caller, start: int, end: int) -> float:
    """Cases scale-case-<start> to scale-case-<end - 1>. Rows a second."""
    began = time.perf_counter()
    for at in range(start, end, 1_000):
        upto = min(at + 1_000, end)
        payload = json.dumps({"cases": [case(i) for i in range(at, upto)]}).encode()
        stats = upload(caller, f"scale-cases-{at}.json", payload)
        if stats.get("cases_imported") != upto - at:
            raise StackFailed(f"import of cases {at} to {upto}: {stats}")
    return (end - start) / (time.perf_counter() - began)


# ---- the four measurements ---------------------------------------------------


def fits(steps: list[dict], count: int, deadline: float | None) -> bool:
    """Would importing up to ``count`` end by the deadline, at the last import's rate?"""
    if deadline is None or not steps:
        return True
    last = steps[-1]
    return last["reached"] and (
        (count - last["count"]) / last["import_rows_per_s"] <= deadline - time.monotonic()
    )


def measure_findings(
    stack: Stack, caller: Caller, steps: list[dict], counts: list[int],
    deadline: float | None = None,
) -> None:
    """Import up to each of ``counts`` and time the reads there. Adds to ``steps``."""
    now = datetime.now(timezone.utc)
    for count in counts:
        if not fits(steps, count, deadline):
            steps.append({"count": count, "reached": False})
            continue
        rate = import_findings(caller, steps[-1]["count"] if steps else 0, count, now)
        stack.sql("ANALYZE")
        print(f"findings: {count:,} stored ({rate:,.0f} rows/s imported)", flush=True)
        reads = {
            "list": timed_read(caller, "/api/v1/findings?limit=100"),
            "summary": timed_read(caller, "/api/v1/findings/stats/summary"),
            "open": timed_read(caller, "/api/v1/findings?state=open&limit=100"),
            "deep": timed_read(caller, f"/api/v1/findings?limit=100&offset={count * 9 // 10}"),
            "search": timed_read(caller, "/api/v1/findings?search=host-0042&limit=100"),
        }
        steps.append({
            "count": count,
            "reached": True,
            "import_rows_per_s": round(rate, 1),
            "reads": reads,
            "responsive": all(
                reads[k]["p95_s"] <= RESPONSIVE_S and not reads[k]["failed"]
                for k in ("list", "summary")
            ),
        })


def measure_cases(
    stack: Stack, caller: Caller, steps: list[dict], counts: list[int],
    deadline: float | None = None,
) -> None:
    for count in counts:
        if not fits(steps, count, deadline):
            steps.append({"count": count, "reached": False})
            continue
        rate = import_cases(caller, steps[-1]["count"] if steps else 0, count)
        stack.sql("ANALYZE")
        print(f"cases: {count:,} stored ({rate:,.0f} rows/s imported)", flush=True)
        reads = {
            "queue": timed_read(caller, "/api/v1/cases?limit=100"),
            "deep": timed_read(caller, f"/api/v1/cases?limit=100&offset={count * 9 // 10}"),
            "search": timed_read(caller, "/api/v1/cases?query=host-0042&limit=100"),
        }
        steps.append({
            "count": count,
            "reached": True,
            "import_rows_per_s": round(rate, 1),
            "reads": reads,
            "responsive": reads["queue"]["p95_s"] <= RESPONSIVE_S and not reads["queue"]["failed"],
        })


def api_mix() -> list[str]:
    """What an outside caller reads: lists, single records, the summary, the trail."""
    one = [f"/api/v1/findings/scale-{(k * 7919) % REFERENCE_FINDINGS:07d}" for k in range(50)]
    held = [f"/api/v1/cases/scale-case-{(k * 7919) % REFERENCE_CASES:06d}" for k in range(50)]
    lists = [
        "/api/v1/findings?limit=50",
        "/api/v1/findings?state=open&severity=critical&limit=50",
        "/api/v1/cases?limit=50",
        "/api/v1/audit/entries?limit=50",
        "/api/v1/findings/stats/summary",
    ]
    # Half the requests are single records, half are the five list reads.
    return [p for k in range(50) for p in (one[k], lists[k % 5], held[k], lists[(k + 2) % 5])]


def measure_api(stack: Stack) -> list[dict]:
    mix, steps = api_mix(), []
    for callers in API_CALLERS:
        seconds: list[float] = []
        failed = [0]
        lock = threading.Lock()
        turn = itertools.count()
        end = time.monotonic() + API_SECONDS

        def work() -> None:
            caller, mine, bad = Caller(stack.api_port, stack.headers()), [], 0
            while time.monotonic() < end:
                status, _, took = caller.request("GET", mix[next(turn) % len(mix)])
                mine.append(took)
                bad += status != 200
            caller.close()
            with lock:
                seconds.extend(mine)
                failed[0] += bad

        began = time.monotonic()
        threads = [threading.Thread(target=work) for _ in range(callers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        took = time.monotonic() - began
        step = {
            "callers": callers,
            "requests": len(seconds),
            "per_s": round(len(seconds) / took, 1),
            "p50_s": round(percentile(seconds, 50), 4),
            "p95_s": round(percentile(seconds, 95), 4),
            "failed": failed[0],
        }
        step["held"] = (
            step["p95_s"] <= RESPONSIVE_S and step["failed"] <= 0.01 * step["requests"]
        )
        print(f"api: {callers} callers, {step['per_s']} req/s, p95 {step['p95_s']} s", flush=True)
        steps.append(step)
    return steps


def measure_intake(stack: Stack) -> dict:
    now = datetime.now(timezone.utc)
    headers = {"Authorization": f"Bearer {stack.webhook_token}"}
    turn = itertools.count()
    sent, refused = [0], [0]
    lock = threading.Lock()
    began = time.monotonic()

    def stored() -> int:
        return int(stack.sql(
            "SELECT count(*) FROM findings WHERE finding_id LIKE 'scale-intake-%'"
        ))

    def work() -> None:
        caller, ok, bad = Caller(stack.intake_port, headers), 0, 0
        while time.monotonic() < began + INTAKE_SECONDS:
            at = next(turn) * INTAKE_BATCH
            batch = [alert("scale-intake", at + k, now, block=128) for k in range(INTAKE_BATCH)]
            status, _, _ = caller.request(
                "POST", "/ingest", json.dumps(batch).encode(), timeout=INTAKE_SECONDS
            )
            ok += status == 200
            bad += status != 200
        caller.close()
        with lock:
            sent[0] += ok * INTAKE_BATCH
            refused[0] += bad

    threads = [threading.Thread(target=work) for _ in range(INTAKE_SENDERS)]
    for t in threads:
        t.start()
    time.sleep(INTAKE_LEAD_S)
    first, first_at = stored(), time.monotonic()
    time.sleep(max(0.0, began + INTAKE_SECONDS - time.monotonic()))
    last, last_at = stored(), time.monotonic()
    for t in threads:
        t.join()
    step = {
        "seconds": round(last_at - first_at, 1),
        "stored": last - first,
        "stored_per_minute": round((last - first) / (last_at - first_at) * 60),
        "accepted": sent[0],
        "refused_pushes": refused[0],
    }
    print(f"intake: {step['stored_per_minute']:,} alerts/min stored", flush=True)
    return step


# ---- the report --------------------------------------------------------------


def last_true(steps: list[dict], flag: str, value: str):
    """The value at the last step, in order, before the first that failed ``flag``."""
    best = None
    for step in steps:
        if not step.get("reached", True) or not step[flag]:
            break
        best = step[value]
    return best


def limits(results: dict) -> list[dict]:
    """The four rows of the limits table: what was measured, and how far it was tested."""
    findings, cases = results["findings"], results["cases"]
    api = results["api"]

    def tested(steps: list[dict], key: str):
        reached = [s[key] for s in steps if s.get("reached", True)]
        return max(reached) if reached else None

    held = [s["per_s"] for s in api if s["held"]]
    rows = [
        {
            "limit": "Open findings with the console still responsive",
            "unit": "findings",
            "measured": last_true(findings, "responsive", "count"),
            "tested_to": tested(findings, "count"),
        },
        {
            "limit": "Open cases at once",
            "unit": "cases",
            "measured": last_true(cases, "responsive", "count"),
            "tested_to": tested(cases, "count"),
        },
        {
            "limit": "/api/v1 requests per second, sustained",
            "unit": "req/s",
            "measured": max(held) if held else None,
            "tested_to": None,
        },
        {
            "limit": "Alerts per minute at intake, sustained",
            "unit": "alerts/min",
            "measured": results["intake"]["stored_per_minute"],
            "tested_to": None,
        },
    ]
    for row in rows:
        # The top step passing means the limit is beyond what was tested.
        row["at_least"] = row["measured"] is not None and row["measured"] == row["tested_to"]
    return rows


def machine() -> dict:
    info = subprocess.run(
        ["docker", "info", "--format", "{{.NCPU}} {{.MemTotal}}"],
        capture_output=True, text=True, check=False,
    ).stdout.split()
    commit = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    return {
        "cpus": int(info[0]) if len(info) == 2 else None,
        "memory_gib": round(int(info[1]) / 1024**3, 1) if len(info) == 2 else None,
        "commit": commit,
        "version": (REPO / "VERSION").read_text().strip(),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


def summary(results: dict) -> str:
    m = results["machine"]
    out = [
        "# Vigil scale limits, measured",
        "",
        f"Vigil {m['version']} at `{m['commit']}`, {m['date']}, on {m['cpus']} CPUs and "
        f"{m['memory_gib']} GiB with every service and the load on that one host. "
        f"Responsive means p95 within {RESPONSIVE_S:g} s. Requests per second and intake "
        f"are with {REFERENCE_FINDINGS:,} findings and {REFERENCE_CASES:,} cases stored.",
        "",
        "| Limit | Measured | Unit |",
        "|---|---|---|",
    ]
    for row in results["limits"]:
        if row["measured"] is None and row["unit"] == "req/s":
            shown = "no number of callers held it"
        elif row["measured"] is None:
            shown = "below the first count tested"
        else:
            shown = f"{row['measured']:,}" + (" or more (the most tested)" if row["at_least"] else "")
        out.append(f"| {row['limit']} | {shown} | {row['unit']} |")

    def ms(read: dict) -> str:
        return f"{read['p95_s'] * 1000:,.0f}" + (" (failed)" if read["failed"] else "")

    out += ["", "## Findings: p95 of each read, in ms", "",
            "| Findings stored | List | Summary | Open only | Deep page | Search | Import rows/s |",
            "|---|---|---|---|---|---|---|"]
    for s in results["findings"]:
        if not s["reached"]:
            out.append(f"| {s['count']:,} | not reached | | | | | |")
            continue
        r = s["reads"]
        out.append(
            f"| {s['count']:,} | {ms(r['list'])} | {ms(r['summary'])} | {ms(r['open'])} | "
            f"{ms(r['deep'])} | {ms(r['search'])} | {s['import_rows_per_s']:,.0f} |"
        )
    out += ["", "## Cases: p95 of each read, in ms", "",
            "| Open cases | Queue | Deep page | Search | Import rows/s |", "|---|---|---|---|---|"]
    for s in results["cases"]:
        if not s["reached"]:
            out.append(f"| {s['count']:,} | not reached | | | |")
            continue
        r = s["reads"]
        out.append(
            f"| {s['count']:,} | {ms(r['queue'])} | {ms(r['deep'])} | {ms(r['search'])} | "
            f"{s['import_rows_per_s']:,.0f} |"
        )
    out += ["", f"## /api/v1 reads, {API_SECONDS} s at each number of callers", "",
            "| Callers at once | Requests/s | p50 ms | p95 ms | Failed |", "|---|---|---|---|---|"]
    for s in results["api"]:
        out.append(
            f"| {s['callers']} | {s['per_s']:,} | {s['p50_s'] * 1000:,.0f} | "
            f"{s['p95_s'] * 1000:,.0f} | {s['failed']:,} |"
        )
    i = results["intake"]
    out += ["", "## Intake at the daemon's webhook", "",
            f"{i['stored']:,} alerts stored in {i['seconds']:g} s with the daemon's queue held "
            f"full. It accepted {i['accepted']:,} over the whole push and refused "
            f"{i['refused_pushes']:,} pushes."]
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.scale.run")
    parser.add_argument("--out", type=Path, default=Path("scale-out"))
    parser.add_argument("--keep", action="store_true", help="leave the stack up afterwards")
    parser.add_argument(
        "--budget-minutes", type=float, default=40.0,
        help="for the counts above the reference point; one that would not fit is skipped",
    )
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    stack = Stack()
    try:
        stack.start()
        stack.sign_in()
        caller = Caller(stack.api_port, stack.headers())
        results = {"machine": machine(), "findings": [], "cases": []}
        findings, cases = results["findings"], results["cases"]
        measure_findings(
            stack, caller, findings, [c for c in FINDING_STEPS if c <= REFERENCE_FINDINGS]
        )
        measure_cases(stack, caller, cases, [c for c in CASE_STEPS if c <= REFERENCE_CASES])
        results["api"] = measure_api(stack)
        results["intake"] = measure_intake(stack)
        # Cases first: they are the shorter import, and findings take what is left.
        deadline = time.monotonic() + args.budget_minutes * 60
        measure_cases(
            stack, caller, cases, [c for c in CASE_STEPS if c > REFERENCE_CASES], deadline
        )
        measure_findings(
            stack, caller, findings, [c for c in FINDING_STEPS if c > REFERENCE_FINDINGS],
            deadline,
        )
        results["limits"] = limits(results)
        (args.out / "results.json").write_text(json.dumps(results, indent=2) + "\n")
        (args.out / "summary.md").write_text(summary(results))
        print(summary(results))
        return 0
    except Exception as exc:
        for service in ("backend", "soc-daemon"):
            (args.out / f"{service}.log").write_text(stack.logs(service, tail="2000"))
        if not isinstance(exc, StackFailed):
            raise
        print(f"scale run failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if not args.keep:
            stack.stop(images=True)


if __name__ == "__main__":
    sys.exit(main())
