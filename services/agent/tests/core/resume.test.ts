import { copyFileSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { beforeEach, describe, expect, it } from "vitest";
import type { RunJob } from "../../contracts/job.js";
import { InProcessLeases } from "../../core/leases.js";
import { InProcessState } from "../../core/state.js";
import { advance, MAX_STALLED_RESUMES, resolveSpec, specOf } from "../../worker.js";
import { SpecError } from "../../core/spec.js";
import { scriptedHarness } from "../support/scripted-harness.js";
import type { ScriptedTurn } from "../support/scripted-provider.js";

const CONCLUDE: ScriptedTurn[] = [{ calls: [] }, { emit: { action: "CONCLUDE", rationale: "done", evidence_citations: [] } }];

const FIXTURES = join(import.meta.dirname, "..", "fixtures");
const RUN = "7d3c2d3e-0000-4000-8000-000000000619";

let config: string;
let leases: InProcessLeases;
beforeEach(() => {
  leases = new InProcessLeases();
  config = join(mkdtempSync(join(tmpdir(), "vigil-resume-")), "vigil.config.yaml");
  copyFileSync(join(FIXTURES, "hunt.config.yaml"), config);
});

// The registry resolves the arch, so the request names only the two layers an
// operator supplies per run.
function startJob(run_kind: RunJob["run_kind"] = "hunt", overrides?: Record<string, unknown>): Extract<RunJob, { reason: "start" }> {
  const tighten = overrides === undefined ? {} : { overrides };
  return {
    schema_version: 1,
    run_id: RUN,
    run_kind,
    tenant_id: null,
    enqueued_at: new Date().toISOString(),
    enqueued_by: "test",
    reason: "start",
    request: { arch: "", playbook: join(FIXTURES, "hunt.playbook.yaml"), config, prompt: "go", ...tighten },
  };
}

function resumeJob(): RunJob {
  return {
    schema_version: 1,
    run_id: RUN,
    run_kind: "hunt",
    tenant_id: null,
    enqueued_at: new Date().toISOString(),
    enqueued_by: "watchdog",
    reason: "resume",
  };
}

function rewriteBudget(iterations: number): void {
  writeFileSync(config, readFileSync(config, "utf8").replace("max_calls: 12", `max_calls: ${iterations}`), "utf8");
}

describe("resolving a run", () => {
  it("routes the run kind through the registry to its arch file", async () => {
    expect((await resolveSpec(startJob())).arch).toBe("threathunt");
  });

  // Startup, not runtime: the kind is resolved before the ledger opens.
  it("refuses a run kind no arch is registered for", async () => {
    await expect(resolveSpec(startJob("tally"))).rejects.toThrow(/no architecture is registered for run_kind tally/);
  });

  it("lets an explicit arch path override the registry's default", async () => {
    const job = startJob();
    job.request.arch = join(import.meta.dirname, "..", "..", "arch", "threathunt.yaml");
    expect((await resolveSpec(job)).dispatch.max_workers).toBe(4);
  });

  // Per-run, so it rides the job rather than the playbook the reference names --
  // which is a definition every run of it shares.
  it("carries what this run asked about into the sections the workflow reads", async () => {
    const job = startJob();
    job.request.hypotheses = ["lateral movement over SMB"];

    expect((await resolveSpec(job)).sections["operator_hypotheses"]).toEqual(["lateral movement over SMB"]);
  });

  it("leaves the sections alone when the run asked about nothing", async () => {
    expect((await resolveSpec(startJob())).sections["operator_hypotheses"]).toBeUndefined();
  });

  // The policy defaults to auto so a headless run advances with nobody at a
  // terminal to ask, which left a console operator no way to be asked at all.
  it("gates the hypotheses on a person when the run asked to be asked", async () => {
    const job = startJob();
    job.request.approve_hypotheses = true;

    const gate = (await resolveSpec(job)).sections["checkpoints"] as Record<string, string>;
    expect(gate["hypothesis_approval"]).toBe("ask");
  });

  it("declares no policy of its own when the run did not ask", async () => {
    expect((await resolveSpec(startJob())).sections["checkpoints"]).toBeUndefined();
  });
});

describe("the arch a run started under is journaled", () => {
  it("writes the resolved spec into the run event", async () => {
    const state = new InProcessState();
    await advance(state, leases, startJob(), scriptedHarness(CONCLUDE));

    const opened = await specOf(state, RUN);
    expect(opened?.arch).toBe("threathunt");
    // The turn count is the one that binds; max_calls is a backstop the hunt
    // raises off it, so this asserts the relationship rather than the arithmetic
    // -- the multiplier is a property of this arch's fan-out, not of the budget.
    // Cast because the harness's budget type has no turn count -- that is the
    // whole point of the split, and the journaled spec carries the hunt's.
    const budgets = opened?.budgets as unknown as { max_iterations: number; max_calls: number; max_cost_usd: number };
    expect(budgets.max_iterations).toBe(8);
    expect(budgets.max_calls).toBeGreaterThan(budgets.max_iterations);
    expect(budgets.max_cost_usd).toBe(5);
  });

  // The whole point of journaling it: the file moved, the run did not.
  it("keeps a resumed run on the spec it opened with after the config is edited", async () => {
    const state = new InProcessState();
    await advance(state, leases, startJob(), scriptedHarness(CONCLUDE));

    const opened = (await specOf(state, RUN))?.budgets.max_calls;

    rewriteBudget(99);
    expect((await resolveSpec(startJob())).budgets.max_calls).toBe(99);

    // Not 99: the run keeps the ceiling it opened with, whatever the file says now.
    await advance(state, leases, resumeJob(), scriptedHarness(CONCLUDE));
    expect((await specOf(state, RUN))?.budgets.max_calls).toBe(opened);
  });

  it("refuses to resume a run that has no ledger", async () => {
    await expect(advance(new InProcessState(), leases, resumeJob(), scriptedHarness(CONCLUDE))).rejects.toThrow(/has no ledger/);
  });
});

// A journaled spec that no longer validates is the same answer every attempt, and
// the retry path released the claim immediately so the sweeper took it again on
// every interval. Two runs did that for five days and left 5,860 failed jobs, none
// of which could ever have succeeded, with nothing anywhere saying why.
describe("a run whose spec cannot be built", () => {
  // Hand-built rather than started: the point is a ledger holding a spec that
  // assembly refuses, which a run that opened successfully cannot have.
  async function ledgerHoldingAnUnbuildableSpec(): Promise<InProcessState> {
    const state = new InProcessState();
    const spec = await resolveSpec(startJob());
    await state.append(RUN, [
      {
        run_id: RUN,
        run_kind: "hunt",
        kind: "run",
        // A ceiling under the budget it caps. The real one was implicit -- the
        // ceiling was a constant while the budget was the caller's -- and stating
        // it here is the same refusal from the same check.
        payload: {
          run_kind: "hunt",
          spec: { ...spec, thresholds: { ...spec.thresholds, hard_max_cost_usd: 0.01 } },
          budgets: spec.budgets,
          seed: RUN,
          tenant_id: null,
          started_by: "test",
        },
      },
    ]);
    return state;
  }

  it("stops instead of handing the claim back for another sweep", async () => {
    const state = await ledgerHoldingAnUnbuildableSpec();

    await expect(advance(state, leases, resumeJob(), scriptedHarness(CONCLUDE))).rejects.toThrow(/hard_max_cost_usd/);

    // finish(), not release(): nothing is left for the sweeper to pick up.
    expect(await leases.sweep(60_000, 10)).toEqual([]);
  });

  it("journals why it stopped, so the reason outlives the failed job", async () => {
    const state = await ledgerHoldingAnUnbuildableSpec();

    await expect(advance(state, leases, resumeJob(), scriptedHarness(CONCLUDE))).rejects.toThrow();

    const terminal = await state.terminal(RUN);
    expect(terminal?.outcome).toBe("failed");
    expect(terminal?.reason).toMatch(/its spec cannot be built/);
  });

  // The terminal is the off switch every other path already relies on, so a sweep
  // that does land finds nothing to do rather than throwing again.
  it("short-circuits a later attempt rather than rebuilding the spec", async () => {
    const state = await ledgerHoldingAnUnbuildableSpec();
    await expect(advance(state, leases, resumeJob(), scriptedHarness(CONCLUDE))).rejects.toThrow();

    await expect(advance(state, leases, resumeJob(), scriptedHarness(CONCLUDE))).resolves.toBeUndefined();
  });
});

describe("what the caller enqueuing a run may tighten", () => {
  it("lowers a ceiling the config set", async () => {
    const spec = await resolveSpec(startJob("hunt", { budgets: { max_cost_usd: 0.5 } }));
    expect(spec.budgets.max_cost_usd).toBe(0.5);
  });

  it("leaves the ceilings it did not name", async () => {
    const plain = await resolveSpec(startJob());
    const tightened = await resolveSpec(startJob("hunt", { budgets: { max_cost_usd: 0.5 } }));
    expect(tightened.budgets.max_calls).toBe(plain.budgets.max_calls);
  });

  it("refuses a ceiling of zero, which is a run that cannot start rather than one that cannot overspend", async () => {
    await expect(resolveSpec(startJob("hunt", { budgets: { max_cost_usd: 0 } }))).rejects.toThrow(/must be a positive number/);
  });

  it("refuses an unknown budget key rather than accepting a knob nothing reads", async () => {
    await expect(resolveSpec(startJob("hunt", { budgets: { max_dollars: 5 } }))).rejects.toThrow(/unknown overrides.budgets key/);
  });

  it("refuses to override anything but budgets and runtime", async () => {
    // The deployment's ceilings are the deployment's; the arch is not negotiable.
    await expect(resolveSpec(startJob("hunt", { roles: {} }))).rejects.toThrow(/may name budgets or runtime/);
  });
});

// The other half of the same disease. A run whose model calls fail without throwing
// never reaches the SpecError path: advance() returns, the sweeper hands it back, and
// it journals a resume and two zero-token spends per attempt forever. One did that
// nineteen times against a gateway holding no API key.
describe("a run that keeps being picked up and going nowhere", () => {
  async function stalledAfter(resumes: number): Promise<InProcessState> {
    const state = new InProcessState();
    await advance(state, leases, startJob(), scriptedHarness(CONCLUDE));
    // Past its own terminal, so the guard rather than the terminal check is what
    // this exercises: a real stall has no terminal, which is the whole problem.
    const fresh = new InProcessState();
    for (const event of (await state.read(RUN)).filter((one) => one.kind !== "terminal")) {
      await fresh.append(RUN, [event]);
    }
    for (let n = 0; n < resumes; n += 1) {
      await fresh.append(RUN, [
        { run_id: RUN, run_kind: "hunt", kind: "resumed", payload: { worker: "w", enqueued_by: "watchdog" } },
        { run_id: RUN, run_kind: "hunt", kind: "spend", payload: { role: "lead", tokens: { input: 0, output: 0, cache_read: 0, cache_write: 0 }, cost_usd: 0, model_id: "m", provider_type: "bifrost", pricing_source: "exact" } },
      ]);
    }
    return fresh;
  }

  it("stops once it has been resumed too many times without progressing", async () => {
    const state = await stalledAfter(MAX_STALLED_RESUMES);

    await advance(state, leases, resumeJob(), scriptedHarness(CONCLUDE));

    const terminal = await state.terminal(RUN);
    expect(terminal?.outcome).toBe("abandoned");
    expect(terminal?.reason).toMatch(/without advancing/);
    expect(await leases.sweep(60_000, 10)).toEqual([]);
  });

  // A run that is merely slow is not a run that is stuck, and ending one early
  // would be worse than the loop this guards.
  it("leaves a run alone while it is still under the cap", async () => {
    const state = await stalledAfter(MAX_STALLED_RESUMES - 1);

    await advance(state, leases, resumeJob(), scriptedHarness(CONCLUDE));

    expect((await state.terminal(RUN))?.outcome).not.toBe("abandoned");
  });

  // Progress resets it: a long hunt writes hundreds of spends and resumes across a
  // week of parking, and the count that matters is since it last did something.
  it("counts only the resumes since the run last did something", async () => {
    const state = await stalledAfter(MAX_STALLED_RESUMES);
    await state.append(RUN, [
      { run_id: RUN, run_kind: "hunt", kind: "dispatch", payload: { role: "threat_hunter" } as never },
    ]);

    await advance(state, leases, resumeJob(), scriptedHarness(CONCLUDE));

    expect((await state.terminal(RUN))?.outcome).not.toBe("abandoned");
  });
});

// Both guards shipped able to kill a run that was doing the one thing a hunt is
// supposed to do when it runs out of road: ask. A parked run is swept on every
// interval and journals a resume each time, and the lead throws a SpecError whose
// reason is the open checkpoint itself -- so the stall count and the unbuildable-spec
// path both fired on a run whose next sweep would have succeeded the moment somebody
// answered. One died that way with an operator's approve already on its way.
describe("a run waiting on a person", () => {
  async function parked(resumes: number): Promise<InProcessState> {
    const state = new InProcessState();
    const spec = await resolveSpec(startJob());
    await state.append(RUN, [
      {
        run_id: RUN, run_kind: "hunt", kind: "run",
        payload: { run_kind: "hunt", spec, budgets: spec.budgets, seed: RUN, tenant_id: null, started_by: "test" },
      },
      {
        run_id: RUN, run_kind: "hunt", kind: "checkpoint",
        payload: { checkpoint_id: "cp-1", checkpoint_class: "budget_anomaly", question: "which index holds CloudTrail?" } as never,
      },
    ]);
    for (let n = 0; n < resumes; n += 1) {
      await state.append(RUN, [
        { run_id: RUN, run_kind: "hunt", kind: "resumed", payload: { worker: "w", enqueued_by: "watchdog" } },
      ]);
    }
    return state;
  }

  it("is not stalling, however many times it has been swept", async () => {
    const state = await parked(MAX_STALLED_RESUMES * 3);

    await advance(state, leases, resumeJob(), scriptedHarness(CONCLUDE)).catch(() => {});

    expect((await state.terminal(RUN))?.outcome).not.toBe("abandoned");
  });

  it("survives the lead throwing because of the very checkpoint it is waiting on", async () => {
    const state = await parked(1);
    const failing = {
      ...scriptedHarness(CONCLUDE),
      build: () => { throw new SpecError("the lead emitted no decision: the ledger holds an open checkpoint, cp-1"); },
    };

    await advance(state, leases, resumeJob(), failing.build as never).catch(() => {});

    // No terminal at all: the run is still answerable, and writing one would have
    // thrown away the answer the operator was in the middle of giving.
    expect(await state.terminal(RUN)).toBeNull();
  });
});
