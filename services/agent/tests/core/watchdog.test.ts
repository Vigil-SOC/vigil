import { copyFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { beforeEach, describe, expect, it } from "vitest";
import type { RunJob } from "../../contracts/job.js";
import { InProcessLeases, LEASE_TTL_MS } from "../../core/leases.js";
import { seedFrom } from "../../core/budget.js";
import { InProcessState } from "../../core/state.js";
import { advance, resolveSpec, sweepOnce, type Enqueue } from "../../worker.js";
import { scriptedHarness } from "../support/scripted-harness.js";
import type { ScriptedTurn } from "../support/scripted-provider.js";

const FIXTURES = join(import.meta.dirname, "..", "fixtures");
const RUN = "7d3c2d3e-0000-4000-8000-000000000633";

const CONCLUDE: ScriptedTurn[] = [{ calls: [] }, { emit: { action: "CONCLUDE", rationale: "done", citations: [] } }];

let config: string;
let state: InProcessState;
let leases: InProcessLeases;
let clock: number;

beforeEach(() => {
  config = join(mkdtempSync(join(tmpdir(), "vigil-watchdog-")), "vigil.config.yaml");
  copyFileSync(join(FIXTURES, "case.config.yaml"), config);
  // Anchored to real time, because the park check and the pool read the process
  // clock. The past is reached by rewinding the store's, never the present.
  clock = Date.now();
  state = new InProcessState(() => clock);
  leases = new InProcessLeases(() => clock);
});

// The real resolved spec, so a resume reads what a resume reads. investigate,
// because the lease and budget machinery under test is the worker's, not a hunt's.
async function opened(
  overrides: Record<string, unknown> | undefined,
  ...rest: readonly Parameters<InProcessState["append"]>[1][number][]
): Promise<void> {
  const spec = await resolveSpec(startJob(overrides));
  await state.append(RUN, [
    {
      run_id: RUN,
      run_kind: "investigate",
      kind: "run",
      payload: { run_kind: "investigate", spec, budgets: spec.budgets, seed: RUN, tenant_id: null, started_by: "test" },
    },
    ...rest,
  ]);
}

function spend(): Parameters<InProcessState["append"]>[1][number] {
  return {
    run_id: RUN,
    run_kind: "investigate",
    kind: "spend",
    payload: { model_id: "m", provider_type: "scripted", role: "lead", tokens: { input: 10, output: 1, cache_read: 0, cache_write: 0 }, cost_usd: null, pricing_source: null, rates: null, fetched_at: null },
  };
}

function startJob(overrides?: Record<string, unknown>): Extract<RunJob, { reason: "start" }> {
  return {
    schema_version: 1,
    run_id: RUN,
    run_kind: "investigate",
    tenant_id: null,
    enqueued_at: new Date().toISOString(),
    enqueued_by: "test",
    reason: "start",
    request: { arch: "", playbook: join(FIXTURES, "case.playbook.yaml"), config, prompt: "go", ...(overrides ? { overrides } : {}) },
  };
}

function resumeJob(): RunJob {
  return {
    schema_version: 1,
    run_id: RUN,
    run_kind: "investigate",
    tenant_id: null,
    enqueued_at: new Date().toISOString(),
    enqueued_by: "watchdog",
    reason: "resume",
  };
}

function recorder(): Enqueue & { jobs: RunJob[]; ids: string[] } {
  const jobs: RunJob[] = [];
  const ids: string[] = [];
  return {
    jobs,
    ids,
    add: async (_name, job, options) => {
      jobs.push(job);
      ids.push(options.jobId);
    },
  };
}

// The exploit this would otherwise ship: a pool built fresh per resume hands a
// killed run a new allowance, and the watchdog turns that into a spend loop.
describe("a resumed run continues its budget rather than restarting it", () => {
  // A run killed mid-iteration leaves spend and no terminal, which is what the
  // watchdog resumes. The fold is all that stands between it and a fresh allowance.
  it("refuses a further call when the ledger's spend fold has reached the cap", async () => {
    await opened({ budgets: { max_calls: 2 } }, spend(), spend());
    expect(await state.terminal(RUN)).toBeNull();

    await advance(state, leases, resumeJob(), scriptedHarness(CONCLUDE));

    // Not one more call: the two on the ledger already spent the allowance.
    expect((await state.read(RUN)).filter((event) => event.kind === "spend")).toHaveLength(2);
    expect((await state.terminal(RUN))?.outcome).toBe("budget_exhausted");
  });

  it("still has its allowance when the fold is short of the cap", async () => {
    await opened({ budgets: { max_calls: 4 } }, spend());

    await advance(state, leases, resumeJob(), scriptedHarness(CONCLUDE));

    expect((await state.read(RUN)).filter((event) => event.kind === "spend").length).toBeGreaterThan(1);
    expect((await state.terminal(RUN))?.outcome).toBe("completed");
  });

  // The pool's wall clock starts at the run event rather than at this process, so
  // a resume does not hand a killed run another half hour.
  it("folds the run's own start time, not the resuming process's", async () => {
    clock -= 120_000;
    await opened(undefined, spend(), spend());
    const started = (await state.read(RUN)).find((event) => event.kind === "run")?.ts;

    const seed = seedFrom(await state.read(RUN));

    expect(seed.spent.calls).toBe(2);
    expect(seed.spent.tokens.input).toBe(20);
    expect(seed.started).toBe(Date.parse(started ?? ""));
  });
});

function checkpoint(id: string): Parameters<InProcessState["append"]>[1][number] {
  return {
    run_id: RUN,
    run_kind: "investigate",
    kind: "checkpoint",
    payload: { checkpoint_id: id, checkpoint_class: "tool_approval", question: "Approve?", raised_at: new Date(clock).toISOString() },
  };
}

function resolution(id: string): Parameters<InProcessState["append"]>[1][number] {
  return {
    run_id: RUN,
    run_kind: "investigate",
    kind: "resolution",
    payload: { checkpoint_id: id, answer: "approve", actor: "someone", text: "go ahead", resolved_at: new Date(clock).toISOString() },
  };
}

const EIGHT_DAYS = 8 * 86_400_000;

describe("a run parked past its TTL is abandoned rather than left parked", () => {
  it("journals a terminal naming how long it actually waited", async () => {
    clock -= EIGHT_DAYS;
    await opened(undefined, checkpoint("apr-unanswered"));
    clock += EIGHT_DAYS;

    await advance(state, leases, resumeJob(), scriptedHarness(CONCLUDE));

    const terminal = await state.terminal(RUN);
    expect(terminal?.outcome).toBe("abandoned");
    expect(terminal?.reason).toBe("parked 8.0 days without an answer");
    // Reaped: a finished run is nobody's work, so nothing sweeps it again.
    clock += LEASE_TTL_MS + 1;
    expect(await leases.sweep(LEASE_TTL_MS, 10)).toEqual([]);
  });

  // The clock only runs while the answer is outstanding, so a checkpoint raised
  // long ago and answered is not a park at all.
  it("leaves a run alone when its checkpoint was answered", async () => {
    clock -= EIGHT_DAYS;
    await opened(undefined, checkpoint("apr-answered"), resolution("apr-answered"));
    clock += EIGHT_DAYS;

    await advance(state, leases, resumeJob(), scriptedHarness(CONCLUDE));

    expect((await state.terminal(RUN))?.outcome).not.toBe("abandoned");
  });

  it("leaves a run alone inside its TTL", async () => {
    clock -= 86_400_000;
    await opened(undefined, checkpoint("apr-unanswered"));
    clock += 86_400_000;

    await advance(state, leases, resumeJob(), scriptedHarness(CONCLUDE));

    expect((await state.terminal(RUN))?.outcome).not.toBe("abandoned");
  });
});

describe("a run nobody is working on is put back on the queue", () => {
  it("enqueues a resume for a lapsed claim and not for a held one", async () => {
    await leases.claim(RUN, "investigate", "worker-a", LEASE_TTL_MS);

    const queue = recorder();
    expect(await sweepOnce(leases, queue)).toBe(0);

    clock += LEASE_TTL_MS + 1;
    expect(await sweepOnce(leases, queue)).toBe(1);
    expect(queue.jobs[0]?.reason).toBe("resume");
    expect(queue.jobs[0]?.run_id).toBe(RUN);
    expect(queue.jobs[0]?.enqueued_by).toBe("watchdog");
  });

  // Claiming is part of discovering, so the run is not handed out twice in the
  // same interval however many sweepers are looking.
  it("hands a due run to exactly one sweeper", async () => {
    await leases.claim(RUN, "investigate", "worker-a", LEASE_TTL_MS);
    clock += LEASE_TTL_MS + 1;

    const [first, second] = [recorder(), recorder()];
    const counts = await Promise.all([sweepOnce(leases, first), sweepOnce(leases, second)]);

    expect(counts.filter((count) => count === 1)).toHaveLength(1);
    expect([...first.ids, ...second.ids]).toHaveLength(1);
  });

  // A parked run's ledger position never moves, so an id derived from it would
  // repeat and the queue would drop every check after the first.
  it("gives each resume its own job id", async () => {
    await leases.claim(RUN, "investigate", "worker-a", LEASE_TTL_MS);
    const queue = recorder();

    clock += LEASE_TTL_MS + 1;
    await sweepOnce(leases, queue);
    clock += LEASE_TTL_MS + 1;
    await sweepOnce(leases, queue);

    expect(queue.ids).toHaveLength(2);
    expect(queue.ids[0]).not.toBe(queue.ids[1]);
  });

  // The join that matters: a sweep is worth nothing unless the worker across the
  // queue can take the run. A sweeper stamping its own name refuses itself.
  it("drives the run when the worker picks the queued resume up", async () => {
    await opened(undefined);
    await leases.claim(RUN, "investigate", "dead-worker", LEASE_TTL_MS);
    clock += LEASE_TTL_MS + 1;

    const queue = recorder();
    expect(await sweepOnce(leases, queue)).toBe(1);
    await advance(state, leases, queue.jobs[0] as RunJob, scriptedHarness(CONCLUDE));

    expect((await state.terminal(RUN))?.outcome).toBe("completed");
  });

  // A run that failed before opening a ledger can neither resume nor journal a
  // terminal, so a row left for it is swept forever. Nothing else drops it.
  it("leaves no lease behind for a start that never opened a ledger", async () => {
    const missing = { ...startJob(), request: { arch: "", playbook: "/nowhere.yaml", config: "/nowhere.yaml", prompt: "go" } };
    await expect(advance(state, leases, missing, scriptedHarness([]))).rejects.toThrow();

    clock += LEASE_TTL_MS + 1;
    expect(await sweepOnce(leases, recorder())).toBe(0);
  });
});

// max_wall_ms is a ceiling on work, and waiting for a person is not work: a seven-day
// park TTL behind a thirty-minute wall budget contradicts itself.
describe("a run does not spend wall time while it is parked", () => {
  it("still has its allowance when the answer comes long after it parked", async () => {
    clock -= 2 * 3_600_000;
    await opened(undefined, checkpoint("apr-late"));
    clock += 2 * 3_600_000;
    await state.append(RUN, [resolution("apr-late")]);

    await advance(state, leases, resumeJob(), scriptedHarness(CONCLUDE));

    expect((await state.terminal(RUN))?.outcome).toBe("completed");
  });

  // Only waiting is excused. A worker that died and was reclaimed spent the hours
  // it spent, which is what stops a crash loop buying a fresh clock every time.
  it("counts the time a run was running but nobody was asked anything", async () => {
    clock -= 2 * 3_600_000;
    await opened(undefined, spend());
    clock += 2 * 3_600_000;

    await advance(state, leases, resumeJob(), scriptedHarness(CONCLUDE));

    expect((await state.terminal(RUN))?.outcome).toBe("budget_exhausted");
  });
});

describe("two workers cannot hold one run", () => {
  it("refuses the second claim while the first is live", async () => {
    expect(await leases.claim(RUN, "investigate", "worker-a", LEASE_TTL_MS)).toBe(true);
    expect(await leases.claim(RUN, "investigate", "worker-b", LEASE_TTL_MS)).toBe(false);
  });

  // Losing the claim is how a worker learns it has been declared dead: it stops
  // rather than paying for a call the ledger will refuse to take.
  it("tells the displaced holder its renewal failed", async () => {
    await leases.claim(RUN, "investigate", "worker-a", LEASE_TTL_MS);
    clock += LEASE_TTL_MS + 1;
    await leases.claim(RUN, "investigate", "worker-b", LEASE_TTL_MS);

    expect(await leases.renew(RUN, "worker-a", LEASE_TTL_MS)).toBe(false);
    expect(await leases.renew(RUN, "worker-b", LEASE_TTL_MS)).toBe(true);
  });

  // A worker that cannot claim returns rather than throwing: someone else driving
  // the run is the good case, and a throw would fill the failed set with non-events.
  it("returns quietly when another worker holds the run", async () => {
    await leases.claim(RUN, "investigate", "worker-a", LEASE_TTL_MS);
    await advance(state, leases, startJob(), scriptedHarness(CONCLUDE));

    expect(await state.latestSeq(RUN)).toBeNull();
  });
});
