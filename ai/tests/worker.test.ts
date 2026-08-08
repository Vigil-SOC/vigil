import { afterAll, beforeEach, describe, expect, it } from "vitest";
import pg from "pg";
import { randomUUID } from "node:crypto";
import { LedgerRepository } from "../ledger/repository.js";
import { advance } from "../worker.js";
import type { RunJob } from "../contracts/job.js";

const pool = new pg.Pool({
  connectionString: process.env["DATABASE_URL"] ?? "postgres://vigil:vigil@localhost:55432/vigil_test",
});
const ledger = new LedgerRepository(pool);

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
    run_kind: "hunt",
    tenant_id: null,
    enqueued_at: new Date().toISOString(),
    enqueued_by: "test",
    reason: "start",
    request: { arch: "arch/threathunt.yaml", playbook: "demo.yaml", config: "vigil.config.yaml", prompt: "go" },
  };
}

// Built rather than spread from a start job: a resume carries no request, and a
// fixture that smuggled one in would not be testing the contract.
function resumeJob(id: string): RunJob {
  return {
    schema_version: 1,
    run_id: id,
    run_kind: "hunt",
    tenant_id: null,
    enqueued_at: new Date().toISOString(),
    enqueued_by: "watchdog",
    reason: "resume",
  };
}

describe("the walking skeleton run", () => {
  it("opens the ledger and marks the run terminal", async () => {
    await advance(ledger, startJob(runId));

    const events = await ledger.read(runId);
    expect(events.map((event) => [event.seq, event.kind])).toEqual([
      [0, "run"],
      [1, "terminal"],
    ]);
    expect(await ledger.terminal(runId)).toMatchObject({ outcome: "completed" });
  });

  it("journals the request into the run event, so a resume needs no other state", async () => {
    await advance(ledger, startJob(runId));

    const [first] = await ledger.read(runId);
    expect(first?.kind).toBe("run");
    expect(first?.payload).toMatchObject({ spec: { arch: "arch/threathunt.yaml" }, started_by: "test" });
  });

  // A crash between the two appends must resume rather than collide on seq 0.
  it("is re-entrant against a ledger that already opened", async () => {
    const job = startJob(runId);
    await ledger.append(runId, 0, [
      {
        run_id: runId,
        run_kind: "hunt",
        kind: "run",
        payload: {
          run_kind: "hunt",
          spec: job.request,
          budgets: { max_iterations: 0, max_cost_usd: 0 },
          seed: runId,
          tenant_id: null,
          started_by: "crashed-worker",
        },
      },
    ]);

    await advance(ledger, resumeJob(runId));

    const events = await ledger.read(runId);
    expect(events.map((event) => event.kind)).toEqual(["run", "terminal"]);
  });

  it("is idempotent when the run already reached terminal", async () => {
    await advance(ledger, startJob(runId));
    await advance(ledger, resumeJob(runId));

    expect(await ledger.read(runId)).toHaveLength(2);
  });

  it("refuses to resume a run that has no ledger", async () => {
    await expect(advance(ledger, resumeJob(runId))).rejects.toThrow(/has no ledger/);
  });
});
