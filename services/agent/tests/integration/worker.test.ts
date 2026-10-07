import { afterAll, beforeEach, describe, expect, it } from "vitest";
import pg from "pg";
import { randomUUID } from "node:crypto";
import { join } from "node:path";
import { LedgerRepository } from "../../ledger/repository.js";
import { LeaseRepository } from "../../ledger/leases.js";
import { advance, resolveSpec, spentOn } from "../../worker.js";
import type { RunJob } from "../../contracts/job.js";
import type { ScriptedTurn } from "../support/scripted-provider.js";
import { scriptedHarness } from "../support/scripted-harness.js";
import { TEST_DSN } from "../support/testdb.js";

const FIXTURES = join(import.meta.dirname, "..", "fixtures");

const pool = new pg.Pool({
  connectionString: TEST_DSN,
});
const ledger = new LedgerRepository(pool);
const leases = new LeaseRepository(pool);

afterAll(() => pool.end());

let runId: string;
beforeEach(() => {
  runId = randomUUID();
});

type StartJob = Extract<RunJob, { reason: "start" }>;

function startJob(id: string): StartJob {
  return {
    schema_version: 1,
    run_id: id,
    run_kind: "investigate",
    tenant_id: null,
    enqueued_at: new Date().toISOString(),
    enqueued_by: "test",
    reason: "start",
    request: {
      arch: "",
      playbook: join(FIXTURES, "case.playbook.yaml"),
      config: join(FIXTURES, "case.config.yaml"),
      prompt: "go",
    },
  };
}

// Built rather than spread from a start job: a resume carries no request, and a
// fixture that smuggled one in would not be testing the contract.
function resumeJob(id: string): RunJob {
  return {
    schema_version: 1,
    run_id: id,
    run_kind: "investigate",
    tenant_id: null,
    enqueued_at: new Date().toISOString(),
    enqueued_by: "watchdog",
    reason: "resume",
  };
}

const CONCLUDE: ScriptedTurn[] = [{ calls: [] }, { emit: { action: "CONCLUDE", rationale: "nothing to pursue", citations: [] } }];

// investigate rather than hunt: what is under test is the worker -- opening a
// ledger, re-entering one, refusing a settled run. The hunt has its own file.
describe("a run reaches its workflow", () => {
  it("opens the ledger and drives the workflow to terminal", async () => {
    await advance(ledger, leases, startJob(runId), scriptedHarness(CONCLUDE));

    const events = await ledger.read(runId);
    expect(events.at(0)?.kind).toBe("run");
    expect(events.at(-1)?.kind).toBe("terminal");
    expect(await ledger.terminal(runId)).toMatchObject({ outcome: "completed" });
  });

  it("journals the resolved spec into the run event, so a resume needs no other state", async () => {
    await advance(ledger, leases, startJob(runId), scriptedHarness(CONCLUDE));

    const [first] = await ledger.read(runId);
    expect(first?.kind).toBe("run");
    expect(first?.payload).toMatchObject({ spec: { arch: "investigate" }, started_by: "test" });
  });

  // A crash between the two appends must resume rather than collide on seq 0.
  it("is re-entrant against a ledger that already opened", async () => {
    const job = startJob(runId);
    await ledger.append(runId, [
      {
        run_id: runId,
        run_kind: "investigate",
        kind: "run",
        payload: {
          run_kind: "investigate",
          spec: await resolveSpec(job),
          budgets: { max_calls: 0, max_cost_usd: 0, max_wall_ms: 600_000, max_park_ms: 604_800_000 },
          seed: runId,
          tenant_id: null,
          started_by: "crashed-worker",
        },
      },
    ]);

    await advance(ledger, leases, resumeJob(runId), scriptedHarness(CONCLUDE));

    const events = await ledger.read(runId);
    expect(events.at(0)?.kind).toBe("run");
    expect(events.at(-1)?.kind).toBe("terminal");
  });

  it("is idempotent when the run already reached terminal", async () => {
    await advance(ledger, leases, startJob(runId), scriptedHarness(CONCLUDE));
    const settled = (await ledger.read(runId)).length;

    // The second call must not reach the workflow at all: the script would run out.
    await advance(ledger, leases, resumeJob(runId), scriptedHarness([]));

    expect(await ledger.read(runId)).toHaveLength(settled);
  });

  it("refuses to resume a run that has no ledger", async () => {
    await expect(advance(ledger, leases, resumeJob(runId), scriptedHarness(CONCLUDE))).rejects.toThrow(/has no ledger/);
  });

  // A resume left no trace at all, so a run that crashed and recovered was
  // indistinguishable from one that never stopped.
  it("marks where a run was picked back up, and by whom", async () => {
    await openLedger(runId);

    await advance(ledger, leases, resumeJob(runId), scriptedHarness(CONCLUDE));
    const marks = (await ledger.read(runId)).filter((event) => event.kind === "resumed");

    expect(marks).toHaveLength(1);
    expect(marks[0]?.payload).toMatchObject({ enqueued_by: "watchdog" });
  });

  // A parked run is swept every interval. A mark per sweep would report several
  // hundred resumes of a run that never moved, which is worse than none.
  it("leaves one mark for a stall it made no progress through", async () => {
    await openLedger(runId);
    await advance(ledger, leases, resumeJob(runId), scriptedHarness([])).catch(() => {});
    await advance(ledger, leases, resumeJob(runId), scriptedHarness([])).catch(() => {});

    expect((await ledger.read(runId)).filter((event) => event.kind === "resumed")).toHaveLength(1);
  });

  // Nothing summed the spend events, so every finished run reported a dash where
  // its cost belongs. A call nobody could price adds nothing rather than a zero.
  it("sums what a run spent from its own spend events", async () => {
    await advance(ledger, leases, startJob(runId), scriptedHarness(CONCLUDE));
    await ledger.append(runId, [
      { run_id: runId, run_kind: "hunt", kind: "spend", payload: spend(0.25) },
      { run_id: runId, run_kind: "hunt", kind: "spend", payload: spend(0.5) },
      { run_id: runId, run_kind: "hunt", kind: "spend", payload: spend(null) },
    ] as never);

    expect(await spentOn(ledger, runId)).toBeCloseTo(0.75);
  });
});

// A ledger a worker opened and did not finish, which is what a resume finds.
async function openLedger(id: string): Promise<void> {
  await ledger.append(id, [
    {
      run_id: id,
      run_kind: "investigate",
      kind: "run",
      payload: {
        run_kind: "investigate",
        spec: await resolveSpec(startJob(id)),
        budgets: { max_calls: 0, max_cost_usd: 0, max_wall_ms: 600_000, max_park_ms: 604_800_000 },
        seed: id,
        tenant_id: null,
        started_by: "crashed-worker",
      },
    },
  ]);
}

function spend(cost: number | null): Record<string, unknown> {
  return {
    model_id: "m",
    provider_type: "p",
    role: "lead",
    tokens: { input: 1, output: 1 },
    cost_usd: cost,
    pricing_source: cost === null ? null : "exact",
  };
}
