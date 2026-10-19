import { afterAll, beforeEach, describe, expect, it } from "vitest";
import pg from "pg";
import { randomUUID } from "node:crypto";
import { join } from "node:path";
import { LedgerRepository } from "../../ledger/repository.js";
import { LeaseRepository } from "../../ledger/leases.js";
import { verifyLedger } from "../../ledger/verify.js";
import { advance } from "../../worker.js";
import type { RunJob } from "../../contracts/job.js";
import type { CheckpointPayload, CallPayload, ChildPayload, RunPayload } from "../../contracts/events.js";
import { replayFrames } from "../../core/replay.js";
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

function startJob(id: string, overrides?: Record<string, unknown>): Extract<RunJob, { reason: "start" }> {
  return {
    schema_version: 1,
    run_id: id,
    run_kind: "investigate",
    tenant_id: null,
    enqueued_at: new Date().toISOString(),
    enqueued_by: "test",
    reason: "start",
    request: { arch: "", playbook: join(FIXTURES, "case.playbook.yaml"), config: join(FIXTURES, "case.config.yaml"), prompt: "go", ...(overrides === undefined ? {} : { overrides }) },
  };
}

function resumeJob(id: string): RunJob {
  return { schema_version: 1, run_id: id, run_kind: "investigate", tenant_id: null, enqueued_at: new Date().toISOString(), enqueued_by: "watchdog", reason: "resume" };
}

const CONCLUDE: ScriptedTurn = {
  emit: { summary: "a scheduled task holds a stale password", disposition: "malicious", confidence: "low", citations: ["r1"], gaps: [], next_steps: [], proposed_actions: [] },
};

describe("investigate runs on the kernel", () => {
  it("drives investigate through its plugin onto a ledger whose chain verifies", async () => {
    await advance(ledger, leases, startJob(runId), scriptedHarness([
      { calls: [{ tool: "load_skill", args: '{"name":"investigate-case"}' }] },
      { calls: [] },
      // Asked once to keep going, having looked at nothing: it stops again.
      { calls: [] },
      CONCLUDE,
    ]));

    const events = await ledger.read(runId);
    expect(isAgentRun(events)).toBe(true);
    expect(events.map((one) => one.kind).filter((kind) => !["spend", "frame", "content"].includes(kind))).toEqual(["run", "call", "conclusion", "review", "terminal"]);
    expect((events.find((one) => one.kind === "call")?.payload as CallPayload).tool).toBe("load_skill");
    expect(await ledger.terminal(runId)).toMatchObject({ outcome: "completed", summary: "a scheduled task holds a stale password" });
    expect(await verifyLedger(pool, runId)).toMatchObject({ ok: true });
  });

  // get_finding never answers here, so r1 is a gap the record refuses as evidence, and a
  // dismissal resting on it is held for a person however the reviewer upholds it.
  it("runs the reviewer as a child run whose own chain verifies, and holds a dismissal over a blind call", async () => {
    // Room past the fixture's six calls: four turns, the ask to keep going, the answer and the review.
    await advance(ledger, leases, startJob(runId, { budgets: { max_calls: 12 } }), scriptedHarness([
      { calls: [{ tool: "get_finding", args: "{}" }] },
      { calls: [{ tool: "propose_hypothesis", args: '{"statement":"a stale scheduled task"}' }] },
      { calls: [{ tool: "link_evidence", args: '{"links":[{"evidence_id":"r1","hypothesis_id":"h1","stance":"supports"}]}' }] },
      { calls: [] },
      { calls: [] },
      { emit: { summary: "a scheduled task holds a stale password", disposition: "benign", confidence: "medium", citations: ["r1"], gaps: [], next_steps: [], proposed_actions: [] } },
      { emit: { stance: "uphold", reasons: "the task is named", missing: [] } },
    ]));

    const events = await ledger.read(runId);
    const child = events.find((one) => one.kind === "child")?.payload as ChildPayload;
    expect(child).toMatchObject({ role: "reviewer", outcome: "completed" });
    const childEvents = await ledger.read(child.child_run_id);
    expect((childEvents[0]?.payload as RunPayload).parent).toMatchObject({ run_id: runId, role: "reviewer" });
    expect(await verifyLedger(pool, runId)).toMatchObject({ ok: true });
    expect(await verifyLedger(pool, child.child_run_id)).toMatchObject({ ok: true });
    // Read back through Postgres, whose jsonb reorders keys: every call still rebuilds.
    expect(replayFrames(events).frames.filter((one) => one.mismatch !== null)).toEqual([]);
    expect(replayFrames(childEvents).frames.filter((one) => one.mismatch !== null)).toEqual([]);
    const calls = events.filter((one) => one.kind === "call").map((one) => one.payload as CallPayload);
    expect(calls.map((one) => [one.tool, one.class])).toEqual([["get_finding", "visibility_gap"], ["propose_hypothesis", "served"], ["link_evidence", "defect"]]);
    expect((await ledger.terminal(runId))?.summary).toContain("held as needs_review because no cited result is linked to a claim on the record");
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
