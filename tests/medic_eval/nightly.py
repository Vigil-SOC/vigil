"""The nightly Medic evaluation (.github/workflows/medic-nightly.yml), on Compose or on Helm.

    python -m tests.medic_eval.nightly --out DIR                    # Compose
    python -m tests.medic_eval.nightly --out DIR --shape helm --cni calico|kindnet

Compose: one stack (compose.eval.yml, project medic-eval, Medic on with auth on), every Compose
cell held for its detect_within_s, scored against Medic's own decision log by
services.medic.eval.score; then the flood, then the survival checks, each in its own
phase so none overlaps a scored cell's window. Medic's containers are sampled
(`docker stats`, once a minute) for the whole run.

Helm: `e2e.sh full` on kind with the CNI (its checks are the privilege checks; its
failure is a failed bring-up), then helm_cells.CELLS on a HelmStack through the same
run_one, scoring, sampling (`crictl stats` on the node) and red rule. No flood and no
survival checks: those stay Compose-only. `--cluster NAME` uses a cluster e2e.sh
already left up (KEEP=1) and leaves it up.

Red (exit 1) only for: bring-up (on Helm, e2e.sh full), inject, verify or revert failing; harm to Vigil;
Medic over 1 vCPU / 1 GiB; a survival check or a flood floor failing. Detection,
time to detect, the lane matrix, false alarms and the advisory bars are reported with
their n and never red. DIR/metrics.json and DIR/summary.md are written on red too.

Verification knobs (the nightly uses none): --only, --hold-cap, --flood-seconds,
--survival, --force CAUSE=verify-fail|miss.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import yaml

from services.medic.eval import score
from tests.medic_eval import flood, medic_api, sample
from tests.medic_eval.inject import config_cells, helm_cells, pipeline_cells, stub_cells, survival_checks
from tests.medic_eval.inject.config_cells import PUSH_EVERY_S
from tests.medic_eval.inject.contract import Cell, run_cell
from tests.medic_eval.inject.helm_stack import E2E, HelmStack
from tests.medic_eval.inject.stack import REPO, Stack

RULES = REPO / "services" / "medic" / "packs" / "medic-core" / "rules"
CELLS = (*config_cells.CELLS, *stub_cells.CELLS, *pipeline_cells.CELLS)
HELM_CELLS = helm_cells.CELLS
# P-2.c holds 8100 s. These run beside it because none touches soc-daemon; every other
# cell recreates or signals it (I-3.a, I-3.b, P-3.a too) and runs on its own.
BESIDE_P2C = ("I-5.b", "L-1.c", "L-2.a", "L-5.a")
JOB_BUDGET_S = 5.5 * 3600  # the job's 6 h, less the report and teardown
BRINGUP_S = 20 * 60  # enable-compose.sh builds three images
HELM_BRINGUP_S = 30 * 60  # e2e.sh full on kind: images loaded, 177 s to Ready in #3900, then its checks
STEP_S = 3 * 60  # a cell's inject, verify and revert around its hold (a daemon recreate each)
FLOOD_PHASE_S = flood.LEAD_S + flood.DURATION_S + flood.SETTLE_S + 120
SURVIVAL_PHASE_S = 30 * 60  # measured ~29 min in #3893
SURVIVAL = ("postgres", "redis", "bifrost", "viewer")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- the rules the pack has -------------------------------------------------


def rule_for(cause: str) -> str | None:
    """The merged rule that names `cause` (or its family: P-1 rules list `P-1`), or None."""
    rules = [yaml.safe_load(p.read_text()) for p in sorted(RULES.glob("*.yaml"))]
    for family in (cause, cause.split(".")[0]):
        for rule in rules:
            if family in rule["fault"]["causes"]:
                # A family entry (P-1) is every rule of the mode: don't name one of them.
                return rule["id"] if family == cause else f"{family} family"
    return None


# ---- senders and priming ----------------------------------------------------


def with_sender(cell: Cell) -> Cell:
    """Push to the daemon's webhook every PUSH_EVERY_S while the fault is held.

    I-5.a and I-5.b's rules count refusals (30 and 20 within 30 minutes); nothing else
    sends. Stack.push presents the pusher's token, which is the fault in I-5.b and the
    right token (to a daemon with none set) in I-5.a.
    """
    box: dict = {}

    def sender(stack: Stack, stop: threading.Event) -> None:
        due = time.monotonic() + PUSH_EVERY_S  # a fixed pace: a push's own time is not added
        while not stop.wait(max(due - time.monotonic(), 0)):
            due += PUSH_EVERY_S
            try:
                stack.push()
            except Exception:  # noqa: BLE001 - the daemon is down or restarting; the next push counts
                pass

    def inject(stack: Stack) -> None:
        cell.inject(stack)
        if "stop" not in box:
            box["stop"] = threading.Event()
            box["thread"] = threading.Thread(target=sender, args=(stack, box["stop"]), daemon=True)
            box["thread"].start()

    def revert(stack: Stack) -> None:
        if "stop" in box:
            box["stop"].set()
            box["thread"].join(timeout=60)
            box.clear()
        cell.revert(stack)

    return dataclasses.replace(cell, inject=inject, revert=revert)


def prime_accepted_push(stack: Stack) -> None:
    """I-5.b: the rule wants a push accepted within 48 h, then none for 30 minutes."""
    finding = {
        "finding_id": f"eval-i5b-{int(time.time())}",
        "title": "medic evaluation: a push that was accepted",
        "severity": "low",
        "description": "",
        "timestamp": now(),
        "data_source": "webhook",
    }
    status, text = stack.http(
        "POST", "soc-daemon", 8081, "/ingest", stack.good_webhook_token, json.dumps([finding])
    )
    assert status == 200 and json.loads(text).get("ingested") == 1, (status, text)


PRIMING = {"I-5.b": prime_accepted_push}
SENDERS = ("I-5.a", "I-5.b")


def settle(stack: Stack) -> None:
    """enable-compose.sh recreates the daemon and the agents to put them on medic-net."""
    stack.wait_daemon()
    for svc in ("agent-serve", "agent-worker", "llm-worker"):
        stack.wait(
            lambda s=svc: stack.compose("ps", "--format", "json", s).stdout,
            lambda o: '"healthy"' in o,
        )


# ---- forcing, for verification only -----------------------------------------


def force(cell: Cell, how: str | None) -> Cell:
    """`verify-fail`: the faulted verify raises. (`miss` is a hold of zero, in run_one.)"""
    if how != "verify-fail":
        return cell

    def verify(stack: Stack, faulted: bool) -> None:
        if faulted:
            raise AssertionError(f"{cell.cause}: verify forced to fail")
        cell.verify(stack, faulted)

    return dataclasses.replace(cell, verify=verify)


# ---- one cell ---------------------------------------------------------------


def run_one(cell: Cell, stack: Stack, out: Path, args, forced: dict) -> dict:
    how = forced.get(cell.cause)
    hold = 0 if how == "miss" else min(cell.detect_within_s, args.hold_cap or 10**9)
    result = {
        "cause": cell.cause, "mode": cell.mode, "lane": cell.lane, "rule": None,
        "detect_within_s": cell.detect_within_s, "hold_s": hold, "forced": how,
        "status": "ok", "error": None, "labels": None,
    }
    try:
        result["rule"] = rule_for(cell.cause)
        use = force(with_sender(cell) if cell.cause in SENDERS else cell, how)
        if cell.cause in PRIMING:
            PRIMING[cell.cause](stack)
        result["labels"] = run_cell(use, stack, out, hold_s=hold)
    except Exception as e:  # noqa: BLE001 - one broken cell does not stop the ones after it
        result["status"] = "failed"
        result["error"] = f"{type(e).__name__}: {e}"[:1500]
        print(traceback.format_exc(), file=sys.stderr)
    print(f"[{now()}] {cell.cause}: {result['status']}", flush=True)
    return result


# ---- Helm bring-up ----------------------------------------------------------


def e2e_full(cluster: str, cni: str, out: Path) -> int:
    """`e2e.sh full` on a new kind cluster, kept for the cells; its checks are the privilege checks."""
    started = time.time()
    log = out / "e2e-full.log"
    with log.open("w") as f:
        done = subprocess.run(
            [str(E2E), "full"], env={**os.environ, "CLUSTER": cluster, "CNI": cni, "KEEP": "1"},
            stdout=f, stderr=subprocess.STDOUT, check=False,
        )
    if done.returncode:
        lines = log.read_text().splitlines()
        why = next((x.strip() for x in lines if "FAIL" in x), lines[-1] if lines else "no output")
        raise RuntimeError(f"e2e.sh full on {cni} exited {done.returncode}: {why} (e2e-full.log has the rest)")
    return round(time.time() - started)


# ---- the schedule -----------------------------------------------------------


def estimate_s(cells: list[Cell], shape: str = "compose") -> float:
    """The wall time of the whole run for `cells`, from their holds."""
    cost = {c.cause: c.detect_within_s + STEP_S for c in cells}
    if shape == "helm":  # one after another, no flood, no survival
        return HELM_BRINGUP_S + sum(cost.values())
    beside = sum(cost[c] for c in BESIDE_P2C if c in cost)
    long_cost = cost.get("P-2.c", 0)
    rest = sum(v for k, v in cost.items() if k not in BESIDE_P2C and k != "P-2.c")
    return BRINGUP_S + max(long_cost, beside) + rest + FLOOD_PHASE_S + SURVIVAL_PHASE_S


def fit(cells: list[Cell], budget_s: float, shape: str = "compose") -> tuple[list[Cell], list[Cell]]:
    """Cells that fit the budget, and the ones dropped (longest hold first)."""
    keep, dropped = list(cells), []
    while keep and estimate_s(keep, shape) > budget_s:
        longest = max(keep, key=lambda c: c.detect_within_s)
        keep.remove(longest)
        dropped.append(longest)
    return keep, dropped


def run_cells(stack: Stack, cells: list[Cell], out: Path, args, forced: dict) -> list[dict]:
    results: list[dict] = []
    lock = threading.Lock()
    faulted = threading.Event()  # P-2.c has recreated the daemon and is holding its fault
    beside_done = threading.Event()  # nothing runs beside P-2.c any more

    def go(cell: Cell) -> None:
        try:
            r = run_one(cell, stack, out, args, forced)
        finally:
            if cell.cause == "P-2.c":
                faulted.set()  # even if it failed, so the others do not wait
        with lock:
            results.append(r)

    beside = [c for c in cells if c.cause in BESIDE_P2C]
    rest = [c for c in cells if c.cause not in BESIDE_P2C and c.cause != "P-2.c"]
    long_cells = []
    for c in cells:
        if c.cause == "P-2.c":
            def verify(s: Stack, up: bool, c=c) -> None:
                c.verify(s, up)
                if up:
                    faulted.set()

            def revert(s: Stack, c=c) -> None:
                beside_done.wait(timeout=2 * 3600)  # its revert recreates the daemon
                c.revert(s)

            # What runs beside it may open its own incidents inside its long window.
            long_cells.append(dataclasses.replace(
                c, verify=verify, revert=revert, may_fire=tuple(sorted({b.mode for b in beside}))
            ))
    # P-2.c in a thread, the cells that leave soc-daemon alone in this one: its inject
    # recreates the daemon, so they start once it is done.
    threads = [threading.Thread(target=go, args=(c,)) for c in long_cells]
    for t in threads:
        t.start()
    if threads:
        faulted.wait(timeout=30 * 60)
    try:
        for c in beside:
            go(c)
    finally:
        beside_done.set()
    for t in threads:
        t.join()
    for c in rest:
        go(c)
    return results


# ---- scoring ----------------------------------------------------------------


def score_cells(stack: Stack, results: list[dict]) -> dict:
    """services.medic.eval.score over the cells that ran, against Medic's decisions."""
    status, records = medic_api.status(stack), medic_api.decisions(stack)
    if not status or records is None:
        return {"error": "Medic's status or decision log could not be read", "cells": [], "metrics": None}
    scored = [r for r in results if r["labels"] and r["rule"]]
    run = score([{"instance_id": status["instance_id"], "labels": r["labels"]} for r in scored], records)
    asdict = dataclasses.asdict(run)
    for r, c in zip(scored, asdict["cells"]):
        r["modes"] = c["modes"]
        r["false_alarms"] = c["false_alarms"]
    return {
        "instance_id": status["instance_id"],
        "records": len(records),
        "scored_cells": [r["cause"] for r in scored],
        "metrics": asdict["metrics"],
    }


# ---- survival ---------------------------------------------------------------


def run_survival(stack: Stack, which: tuple[str, ...]) -> list[dict]:
    out = []
    for name in which:
        started = time.time()
        try:
            if name == "viewer":
                got = survival_checks.check_viewer_revoked(stack)
            else:
                got = survival_checks.check_dependency_down(stack, name)
            out.append({"check": name, "status": "ok", "seconds": round(time.time() - started), **got})
        except Exception as e:  # noqa: BLE001
            print(traceback.format_exc(), file=sys.stderr)
            out.append({"check": name, "status": "failed", "seconds": round(time.time() - started),
                        "error": f"{type(e).__name__}: {e}"[:1500]})
        print(f"[{now()}] survival {name}: {out[-1]['status']}", flush=True)
    return out


# ---- harm to Vigil ----------------------------------------------------------


def vigil_harm(stack: Stack) -> list[str]:
    """Vigil is as it was before the run: the backend answers and its services run."""
    problems = []
    try:
        if stack.http("GET", "backend", 6987, "/api/health")[0] != 200:
            problems.append("the backend's /api/health is not 200")
    except Exception as e:  # noqa: BLE001
        problems.append(f"the backend could not be asked: {e}")
    try:
        problems += [f"{svc} is not running" for svc in stack.not_running()]
    except Exception as e:  # noqa: BLE001
        problems.append(f"the services could not be listed: {e}")
    return problems


# ---- the report -------------------------------------------------------------


def summary(m: dict) -> str:
    rate = (m["scoring"].get("metrics") or {}).get("detection_rate") or {}
    ttd = (m["scoring"].get("metrics") or {}).get("time_to_detect_s") or {}
    lines = [
        f"# Medic nightly evaluation ({m['shape']}): {'RED' if m['verdict']['red'] else 'green'}",
        "",
        *(f"- red: {r}" for r in m["verdict"]["reasons"]),
        "",
        *([f"Bring-up: e2e.sh full on {m['cni']}, {m['e2e_s']} s (its privilege checks passed)."] if m.get("e2e_s") else []),
        f"Detection rate (on time, n={rate.get('n')}): {rate.get('hits')}/{rate.get('n')}. "
        f"Median time to detect (n={ttd.get('n')}): {ttd.get('median')} s. Reported, never red.",
        "",
        "| cell | rule | held s | result | detection |",
        "|---|---|---|---|---|",
    ]
    for c in sorted(m["cells"], key=lambda c: c["cause"]):
        if c.get("modes"):
            how = ", ".join(f"{x['outcome']} ({x['time_to_detect_s']} s)" for x in c["modes"])
        else:
            how = "no rule" if c.get("skipped") == "no rule" else c.get("skipped") or ""
        lines.append(f"| {c['cause']} | {c['rule'] or '-'} | {c['hold_s']} | {c['status']} | {how} |")
    f = m["flood"]
    only = " (Compose only)" if m["shape"] == "helm" else ""
    lines += ["", "Flood: " + ("not run" + only if not f else "; ".join(f["failures"]) or "floors held")]
    checks = (f"{s['check']} {s['status']}" + (f" (restored {', '.join(s['restored'])})" if s.get("restored") else "")
              for s in m["survival"])
    lines += ["", "Survival: " + (", ".join(checks) or "not run" + only)]
    return "\n".join(lines) + "\n"


def resources(sampler: sample.Sampler, store: list, started: float) -> dict:
    stack = sampler.stack
    kib = [k for k in store if k is not None]
    elapsed = time.time() - started
    growth = (kib[-1] - kib[0]) / 1024 / elapsed * 86400 if len(kib) == 2 and elapsed > 0 else None
    return {
        "interval_s": sampler.interval_s,
        "stats_failures": sampler.failures,
        "usage": sample.summarize(sampler.samples, stack.medic_services),
        "store_kib": store,
        "store_n": len(kib),
        "store_elapsed_s": round(elapsed),
        "store_growth_mib_per_day": None if growth is None else round(growth, 2),
        "advisory_bars": sample.ADVISORY,
        "hard_limits": {"cpu_vcpu": sample.HARD_CPU_VCPU, "rss_mib": sample.HARD_RSS_MIB},
    }


def verdict(m: dict) -> dict:
    reasons = []
    for c in m["cells"]:
        if c["status"] == "failed":
            reasons.append(f"cell {c['cause']}: {c['error']}")
    if m["bringup_error"]:
        reasons.append(f"run stopped early: {m['bringup_error']}")
    reasons += [f"harm to Vigil: {p}" for p in m["vigil_harm"]]
    for svc, u in m["resources"]["usage"].items():
        if u["n"] and u["cpu_p95_vcpu"] > sample.HARD_CPU_VCPU + sample.CPU_TOLERANCE:
            reasons.append(f"{svc} p95 CPU {u['cpu_p95_vcpu']} vCPU over {sample.HARD_CPU_VCPU} (n={u['n']})")
        if u["n"] and u["rss_peak_mib"] > sample.HARD_RSS_MIB:
            reasons.append(f"{svc} peak memory {u['rss_peak_mib']} MiB over {sample.HARD_RSS_MIB:.0f} (n={u['n']})")
    reasons += [f"flood: {f}" for f in (m["flood"] or {}).get("failures", [])]
    reasons += [f"survival {s['check']}: {s['error']}" for s in m["survival"] if s["status"] == "failed"]
    return {"red": bool(reasons), "reasons": reasons}


def parse_forced(items: list[str]) -> dict:
    forced = {}
    for item in items:
        cause, _, how = item.partition("=")
        if how not in ("verify-fail", "miss"):
            sys.exit(f"--force {item}: want CAUSE=verify-fail or CAUSE=miss")
        forced[cause] = how
    return forced


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--shape", choices=("compose", "helm"), default="compose")
    p.add_argument("--cni", choices=("calico", "kindnet"), default="calico", help="helm: the kind cluster's CNI")
    p.add_argument("--cluster", help="helm: a kind cluster e2e.sh full left up; used as it is and kept")
    p.add_argument("--budget-s", type=float, default=JOB_BUDGET_S)
    p.add_argument("--sample-interval", type=float, default=60)
    p.add_argument("--only", help="comma-separated causes (verification only)")
    p.add_argument("--hold-cap", type=float, default=0, help="cap every hold, seconds (verification only)")
    p.add_argument("--flood-seconds", type=int, default=flood.DURATION_S)
    p.add_argument("--survival", default=",".join(SURVIVAL), help="comma-separated checks, or none")
    p.add_argument("--force", action="append", default=[], metavar="CAUSE=verify-fail|miss")
    args = p.parse_args(argv)
    forced = parse_forced(args.force)
    survival = tuple(s for s in args.survival.split(",") if s and s != "none")

    helm = args.shape == "helm"
    cells = [c for c in (HELM_CELLS if helm else CELLS) if not args.only or c.cause in args.only.split(",")]
    cells, long = fit(cells, args.budget_s, args.shape)
    args.out.mkdir(parents=True, exist_ok=True)
    traces = args.out / "traces"
    traces.mkdir(exist_ok=True)

    m: dict = {
        "shape": args.shape, "cni": args.cni if helm else None,
        "started_at": now(), "commit": os.environ.get("GITHUB_SHA"), "estimate_s": estimate_s(cells, args.shape),
        "budget_s": args.budget_s, "cells": [], "scoring": {}, "flood": None, "survival": [],
        "resources": {}, "vigil_harm": [], "bringup_error": None,
        "too_long": [
            {"cause": c.cause, "mode": c.mode, "lane": c.lane, "rule": rule_for(c.cause), "detect_within_s": c.detect_within_s,
             "hold_s": 0, "status": "skipped", "error": None, "labels": None, "skipped": "too long for the nightly"}
            for c in long
        ],
    }
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="medic-nightly-") as home:
        stack = sampler = None
        store: list = []
        try:
            if helm:
                cluster = args.cluster or f"medic-helm-{args.cni}"
                stack = HelmStack(cluster)
                if not args.cluster:
                    m["e2e_s"] = e2e_full(cluster, args.cni, args.out)
                stack.start()  # the stub, the daemon's token, the first admin
            else:
                stack = Stack(Path(home))
                stack.start()
                settle(stack)
            sampler = sample.Sampler(stack, args.sample_interval)
            sampler.start()
            store.append(sample.store_kib(stack))
            m["cells"] = run_cells(stack, cells, traces, args, forced)
            for r in m["cells"]:
                if not r["rule"]:
                    r["skipped"] = "no rule"  # runs inject/verify/revert; not scored
            # Scoring is report-only: its failure is not red, and the floors still run.
            try:
                m["scoring"] = score_cells(stack, m["cells"])
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                m["scoring"] = {"error": f"{type(e).__name__}: {e}"[:1500], "metrics": None}
            if not helm:
                try:
                    m["flood"] = flood.run_flood(stack, args.flood_seconds)
                except Exception as e:  # noqa: BLE001 - the flood failing to run is a floor missed
                    traceback.print_exc()
                    m["flood"] = {"failures": [f"the flood phase did not complete: {type(e).__name__}: {e}"[:1500]]}
                m["survival"] = run_survival(stack, survival)
            m["vigil_harm"] = vigil_harm(stack)
            store.append(sample.store_kib(stack))
        except Exception as e:  # noqa: BLE001 - bring-up or an unexpected failure: red, with whatever was gathered
            print(traceback.format_exc(), file=sys.stderr)
            err = f"{type(e).__name__}: {e}"
            # The cause and exit code are at the end; keep it, bounded.
            m["bringup_error"] = err if len(err) <= 4000 else f"{err[:300]} … {err[-3600:]}"
        finally:
            if sampler:
                sampler.stop()
            m["resources"] = resources(sampler, store, started) if sampler else {"usage": {}}
            m["finished_at"] = now()
            m["cells"] += m["too_long"]
            m["verdict"] = verdict(m)
            (args.out / "metrics.json").write_text(json.dumps(m, indent=2, default=str) + "\n")
            (args.out / "summary.md").write_text(summary(m))
            try:
                if stack and not (helm and args.cluster):  # a cluster handed in is kept
                    stack.stop()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
    print((args.out / "summary.md").read_text())
    return 1 if m["verdict"]["red"] else 0


if __name__ == "__main__":
    sys.exit(main())
