import { afterAll, beforeEach, describe, expect, it } from "vitest";
import pg from "pg";
import { randomUUID } from "node:crypto";
import { join } from "node:path";
import { LedgerRepository } from "../../ledger/repository.js";
import { LeaseRepository } from "../../ledger/leases.js";
import { verifyLedger } from "../../ledger/verify.js";
import { advance } from "../../worker.js";
import type { RunJob } from "../../contracts/job.js";
import type { CheckpointPayload, CallPayload } from "../../contracts/events.js";
import { isAgentRun } from "../../core/agent.js";
import { scriptedHarness } from "../support/scripted-harness.js";
import type { ScriptedTurn } from "../support/scripted-provider.js";
import { TEST_DSN } from "../support/testdb.js";

const FIXTURES = join(import.meta.dirname, "..", "fixtures");
const pool = new pg.Pool({ connectionString: TEST_DSN });
const ledger = new LedgerRepository(pool);
const leases = new LeaseRepository(pool);

afterAll(() => pool.end());

let runId: string;
beforeEach(() => {
  runId = randomUUID();
});

function startJob(id: string): Extract<RunJob, { reason: "start" }> {
  return {
    schema_version: 1,
    run_id: id,
    run_kind: "investigate",
    tenant_id: null,
    enqueued_at: new Date().toISOString(),
    enqueued_by: "test",
    reason: "start",
    request: { arch: "", playbook: join(FIXTURES, "case.playbook.yaml"), config: join(FIXTURES, "case.config.yaml"), prompt: "go" },
  };
}

function resumeJob(id: string): RunJob {
  return { schema_version: 1, run_id: id, run_kind: "investigate", tenant_id: null, enqueued_at: new Date().toISOString(), enqueued_by: "watchdog", reason: "resume" };
}

const CONCLUDE: ScriptedTurn = {
  emit: { summary: "a scheduled task holds a stale password", disposition: "malicious", confidence: "low", citations: ["r1"], gaps: [], next_steps: [] },
};

describe("investigate runs on the kernel", () => {
  it("drives investigate through its plugin onto a ledger whose chain verifies", async () => {
    await advance(ledger, leases, startJob(runId), scriptedHarness([
      { calls: [{ tool: "load_skill", args: '{"name":"investigate-case"}' }] },
      { calls: [] },
      CONCLUDE,
    ]));

    const events = await ledger.read(runId);
    expect(isAgentRun(events)).toBe(true);
    expect(events.map((one) => one.kind).filter((kind) => kind !== "spend")).toEqual(["run", "call", "conclusion", "review", "terminal"]);
    expect((events.find((one) => one.kind === "call")?.payload as CallPayload).tool).toBe("load_skill");
    expect(await ledger.terminal(runId)).toMatchObject({ outcome: "completed", summary: "a scheduled task holds a stale password" });
    expect(await verifyLedger(pool, runId)).toMatchObject({ ok: true });
  });

  it("parks on a gated call and resumes on the kernel once it is answered", async () => {
    // case_records is gated in case.config.yaml, so the run parks on it.
    await advance(ledger, leases, startJob(runId), scriptedHarness([{ calls: [{ tool: "case_records", args: '{"case_id":"c-1"}' }] }]));
    const checkpoint = (await ledger.read(runId)).find((one) => one.kind === "checkpoint")?.payload as CheckpointPayload;
    expect(checkpoint.checkpoint_id).toBeTypeOf("string");

    await ledger.append(runId, [{
      run_id: runId,
      run_kind: "investigate",
      kind: "resolution",
      payload: { checkpoint_id: checkpoint.checkpoint_id, actor: "analyst", answer: "approve", text: "", resolved_at: new Date().toISOString() },
    }]);
    await advance(ledger, leases, resumeJob(runId), scriptedHarness([
      { calls: [{ tool: "case_records", args: '{"case_id":"c-1"}' }] },
      { calls: [] },
      CONCLUDE,
    ]));

    const events = await ledger.read(runId);
    expect(events.some((one) => (one.kind as string) === "decision")).toBe(false);
    expect(events.filter((one) => one.kind === "call")).toHaveLength(1);
    expect(await ledger.terminal(runId)).toMatchObject({ outcome: "completed" });
    expect(await verifyLedger(pool, runId)).toMatchObject({ ok: true });
  });

});
