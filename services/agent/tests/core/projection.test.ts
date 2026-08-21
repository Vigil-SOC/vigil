import { describe, expect, it } from "vitest";
import { archFor } from "../../arch/registry.js";
import type { AgentEvent, NewEvent } from "../../contracts/events.js";
import { TOOL_APPROVAL } from "../../core/loop.js";
import { InProcessState } from "../../core/state.js";
import { leadProjection } from "../../workflows/lead/projection.js";
import type { LeadKinds } from "../../workflows/lead/workflow.js";

const RUN = "7d3c2d3e-0000-4000-8000-000000000629";
const CHECKPOINT = "apr-c0ffee";

type New = NewEvent<LeadKinds>;

const event = (kind: New["kind"], payload: New["payload"]): New => ({ run_id: RUN, run_kind: "investigate", kind, payload });

const opened = (): New =>
  event("run", { run_kind: "investigate", spec: {}, budgets: { max_calls: 8, max_cost_usd: 5, max_wall_ms: 600_000, max_park_ms: 604_800_000 }, seed: RUN, tenant_id: null, started_by: "daemon" });
const decided = (action: string, rationale = "because"): New => event("decision", { action, rationale, worker: null });
const found = (answer: unknown): New => event("finding", { agent_id: "lead", answer });
const spent = (cost_usd: number | null): New =>
  event("spend", { model_id: "m", provider_type: "bifrost", role: "lead", tokens: { input: 1, output: 1, cache_read: 0, cache_write: 0 }, cost_usd, pricing_source: null });
const parked = (checkpoint_id = CHECKPOINT): New =>
  event("checkpoint", { checkpoint_id, checkpoint_class: TOOL_APPROVAL, question: "isolate the host?", raised_at: "2026-08-12T00:00:00Z" });
const answered = (answer: "approve" | "reject", checkpoint_id = CHECKPOINT): New =>
  event("resolution", { checkpoint_id, actor: "analyst", answer, text: "", resolved_at: "2026-08-12T00:01:00Z" });
const ended = (outcome: "completed" | "failed", reason: string): New => event("terminal", { outcome, reason });

async function project(...events: readonly New[]) {
  const state = new InProcessState<LeadKinds>();
  await state.append(RUN, events);
  return leadProjection(RUN, await state.read(RUN));
}

describe("what a supervisor is told about a run", () => {
  it("reports a run still going as running, with nothing parked", async () => {
    const projection = await project(opened(), decided("EXAMINE"));

    expect(projection.status).toBe("running");
    expect(projection.outcome).toBeNull();
    expect(projection.iterations).toBe(1);
    expect(projection.open_checkpoint).toBeNull();
  });

  it("reports how it ended, and why", async () => {
    const projection = await project(opened(), decided("CONCLUDE"), ended("completed", "the analyst answered"));

    expect(projection.status).toBe("terminal");
    expect(projection.outcome).toBe("completed");
    expect(projection.reason).toBe("the analyst answered");
  });

  it("counts iterations as decisions, not as model calls", async () => {
    // Two calls per decision at least -- a tool turn and an emission -- so a
    // supervisor enforcing max_iterations would stop early on the wrong count.
    const projection = await project(opened(), spent(0.1), decided("EXAMINE"), spent(0.1), spent(0.1), decided("CONCLUDE"));
    expect(projection.iterations).toBe(2);
  });

  it("carries the findings the run produced", async () => {
    const projection = await project(opened(), found({ verdict: "benign" }));
    expect(projection.findings).toEqual([{ agent_id: "lead", answer: { verdict: "benign" } }]);
  });
});

describe("cost, as the gateway reported it", () => {
  it("sums what was priced", async () => {
    expect((await project(opened(), spent(0.25), spent(0.5))).cost_usd).toBe(0.75);
  });

  it("stays null when nothing was priced, because that is not the same as zero", async () => {
    // A run that spent nothing and a run whose provider priced nothing are
    // different claims, and a budget guard must not read the second as the first.
    expect((await project(opened(), spent(null), spent(null))).cost_usd).toBeNull();
  });
});

describe("the checkpoint a resolution must answer", () => {
  it("reports a parked run as waiting, and names what it is waiting on", async () => {
    const projection = await project(opened(), decided("EXAMINE"), parked());

    expect(projection.status).toBe("waiting_approval");
    expect(projection.open_checkpoint?.checkpoint_id).toBe(CHECKPOINT);
    expect(projection.open_checkpoint?.question).toBe("isolate the host?");
  });

  it("goes back to running once the checkpoint is answered", async () => {
    const projection = await project(opened(), parked(), answered("approve"));

    expect(projection.status).toBe("running");
    expect(projection.open_checkpoint).toBeNull();
  });

  it("treats a rejection as answered: the run resumes and declines the call", async () => {
    // Rejected is not still-waiting. A supervisor that read it as waiting would
    // raise the same approval again, forever.
    expect((await project(opened(), parked(), answered("reject"))).open_checkpoint).toBeNull();
  });

  it("names the oldest unanswered one when more than one was raised", async () => {
    const projection = await project(opened(), parked("apr-first"), answered("approve", "apr-first"), parked("apr-second"));
    expect(projection.open_checkpoint?.checkpoint_id).toBe("apr-second");
  });

  it("reports a terminal run as terminal even with a checkpoint left open", async () => {
    // An abandoned run parks nobody: a supervisor must not keep asking for an
    // approval to a run that already ended.
    const projection = await project(opened(), parked(), ended("failed", "the budget refused another iteration"));
    expect(projection.status).toBe("terminal");
  });
});

describe("the registry says which kinds can be read", () => {
  it("gives investigate a projection", () => {
    expect(archFor("investigate").projection).toBeTypeOf("function");
  });

  it("gives chat none, because a conversation is the transcript the client holds", () => {
    expect(archFor("chat").projection).toBeUndefined();
  });

  it("reaches the same answer through the registry as through the workflow", async () => {
    const state = new InProcessState<LeadKinds>();
    await state.append(RUN, [opened(), decided("EXAMINE")]);
    const events = (await state.read(RUN)) as readonly AgentEvent<Record<never, never>>[];

    expect(archFor("investigate").projection!(RUN, events)).toEqual(leadProjection(RUN, await state.read(RUN)));
  });
});
