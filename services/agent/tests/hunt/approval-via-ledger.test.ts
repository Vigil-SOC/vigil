import { copyFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { CheckpointPayload } from "../../contracts/events.js";
import type { RunJob } from "../../contracts/job.js";
import { InProcessLeases } from "../../core/leases.js";
import { InProcessState } from "../../core/state.js";
import { advance } from "../../worker.js";
import { scriptedHarness } from "../support/scripted-harness.js";
import type { ScriptedTurn } from "../support/scripted-provider.js";

// The console's path, not a directive's: approving the gate records the answer on the
// backend, the resume job reads it from /decisions, and journalAnswers puts it on the
// ledger. Nothing ever drains a directive, so only the controller's own reconcile of
// an answered start gate can release the hunt.
const FIXTURES = join(import.meta.dirname, "..", "fixtures");
const CONCLUDE: ScriptedTurn[] = [{ calls: [] }, { emit: { action: "CONCLUDE", rationale: "done", evidence_citations: [] } }];

let config: string;
let leases: InProcessLeases;
let decisions: unknown[];
let posted: { url: string; body: unknown }[];
const real = globalThis.fetch;

beforeEach(() => {
  leases = new InProcessLeases();
  decisions = [];
  posted = [];
  config = join(mkdtempSync(join(tmpdir(), "vigil-gate-")), "vigil.config.yaml");
  copyFileSync(join(FIXTURES, "hunt.config.yaml"), config);
  process.env["VIGIL_RUNS_URL"] = "http://backend/internal/runs";
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    if (init?.body !== undefined) posted.push({ url: String(url), body: JSON.parse(String(init.body)) });
    return { ok: true, status: 200, json: async () => ({ decisions }) } as Response;
  }) as unknown as typeof globalThis.fetch;
});

afterEach(() => {
  globalThis.fetch = real;
  delete process.env["VIGIL_RUNS_URL"];
});

function job(runId: string, reason: "start" | "resume"): RunJob {
  const base = { schema_version: 1, run_id: runId, run_kind: "hunt", tenant_id: null, enqueued_at: new Date().toISOString(), enqueued_by: "test" } as const;
  if (reason === "resume") return { ...base, reason };
  return {
    ...base,
    reason,
    request: { arch: "", playbook: join(FIXTURES, "hunt.playbook.yaml"), config, prompt: "go", approve_hypotheses: true },
  };
}

async function parkedAtGate(runId: string): Promise<{ state: InProcessState; checkpointId: string }> {
  const state = new InProcessState();
  await advance(state, leases, job(runId, "start"), scriptedHarness(CONCLUDE));
  const raised = (await eventsOf(state, runId)).find((event) => event.kind === "checkpoint");
  expect((raised?.payload as unknown as CheckpointPayload).checkpoint_class).toBe("hypothesis_approval");
  expect((await eventsOf(state, runId)).some((event) => event.kind === "decision")).toBe(false);
  return { state, checkpointId: (raised?.payload as unknown as CheckpointPayload).checkpoint_id };
}

// The generic State types only the kinds every run shares; the hunt's own are read untyped.
const eventsOf = async (state: InProcessState, runId: string) =>
  (await state.read(runId)) as unknown as { kind: string; payload: Record<string, unknown> }[];

const answer = (checkpoint_id: string, verdict: "approve" | "reject") => ({
  checkpoint_id,
  answer: verdict,
  actor: "analyst",
  text: "",
  directive_id: null,
  resolved_at: new Date().toISOString(),
});

describe("approving a hunt's hypothesis gate through the ledger", () => {
  it("starts the hunt: it reaches iteration 1 and the run row reads running", async () => {
    const runId = "7d3c2d3e-0000-4000-8000-000000001914";
    const { state, checkpointId } = await parkedAtGate(runId);

    decisions = [answer(checkpointId, "approve")];
    await advance(state, leases, job(runId, "resume"), scriptedHarness(CONCLUDE));

    const events = await eventsOf(state, runId);
    const iterations = events.filter((event) => event.kind === "decision").map((event) => event.payload["iteration"]);
    expect(iterations).toContain(1);
    expect(posted.find((one) => one.url.endsWith(`${runId}/status`) && (one.body as { status: string }).status === "running")).toBeDefined();
  });

  it("ends a rejected hunt as aborted instead of starting it", async () => {
    const runId = "7d3c2d3e-0000-4000-8000-000000001915";
    const { state, checkpointId } = await parkedAtGate(runId);

    decisions = [answer(checkpointId, "reject")];
    await advance(state, leases, job(runId, "resume"), scriptedHarness(CONCLUDE));

    const events = await eventsOf(state, runId);
    expect(events.some((event) => event.kind === "decision")).toBe(false);
    expect((await state.terminal(runId))?.outcome).toBe("aborted");
  });
});
