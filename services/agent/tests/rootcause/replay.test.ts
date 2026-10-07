import { describe, expect, it } from "vitest";
import type { AgentEvent } from "../../contracts/events.js";
import { PROVER_TOOL, type RootCauseKinds, type StepPayload } from "../../workflows/rootcause/proof.js";
import { rootCauseReplay } from "../../workflows/rootcause/replay.js";

const RUN = "5a2c2d3e-0000-4000-8000-000000000c40";

let seq = 0;
function event(kind: string, payload: unknown): AgentEvent<RootCauseKinds> {
  seq += 1;
  const ts = `2024-01-02T03:00:${String(seq).padStart(2, "0")}Z`;
  return { run_id: RUN, run_kind: "root_cause", seq, ts, kind, payload } as unknown as AgentEvent<RootCauseKinds>;
}

function step(over: Partial<StepPayload>): StepPayload {
  return {
    step_id: "step-1",
    event: "beacon to 203.0.113.5",
    who: "",
    at: "2024-01-02T02:00:00Z",
    link: "",
    artifact: "",
    cause_id: null,
    origin: false,
    link_status: "none",
    origin_status: "none",
    ...over,
  };
}

function search(args: string, result: unknown, tool = PROVER_TOOL): AgentEvent<RootCauseKinds> {
  return event("dispatch", {
    dispatch_id: `dsp-${seq}`,
    agent_id: "lead",
    status: "complete",
    question_id: null,
    failure_reason: null,
    result,
    calls: [{ tool, arguments: args }],
  });
}

const found = (rows: unknown[]) => ({ ok: true, rows, rowCount: rows.length, capped: false, sourceSystem: "splunk" });
const down = { ok: false, failure: { kind: "unavailable", detail: "splunk is down" } };

describe("the root-cause replay", () => {
  it("lists searches, steps and notices in ledger order, a revised step twice", () => {
    const replay = rootCauseReplay(RUN, [
      event("run", { run_kind: "root_cause", budgets: { max_calls: 1024, max_cost_usd: 15, max_wall_ms: 5_400_000, other: 1 } }),
      event("spend", { role: "lead", tokens: {}, cost_usd: 0.01 }),
      search("index=a", found([{ results: [{ a: 1 }, { a: 2 }] }])),
      event("step", step({ link_status: "unproven" })),
      search("index=b", down),
      event("step", step({ link: "a.exe", cause_id: "step-0", link_status: "proven" })),
      event("notice", { text: "no flow logs" }),
    ]);
    expect(replay).toMatchObject({ run_id: RUN, run_kind: "root_cause", budgets: { max_calls: 1024, max_cost_usd: 15, max_wall_ms: 5_400_000 } });
    expect(replay.budgets).not.toHaveProperty("other");
    expect(replay.steps.map((s) => s.kind)).toEqual(["search", "step", "search", "step", "notice"]);
    expect(replay.steps[0]).toEqual({ kind: "search", recorded_at: "2024-01-02T03:00:03Z", tool: PROVER_TOOL, args: "index=a", rows: 2, failed: false });
    expect(replay.steps[2]).toMatchObject({ kind: "search", failed: true, rows: 0, failure: "unavailable" });
    // the envelope time and the step's own claimed time stay apart
    expect(replay.steps[1]).toMatchObject({ kind: "step", step_id: "step-1", recorded_at: "2024-01-02T03:00:04Z", at: "2024-01-02T02:00:00Z" });
    expect(replay.steps[3]).toMatchObject({ kind: "step", step_id: "step-1", link: "a.exe", cause_id: "step-0", link_status: "proven" });
    expect(replay.steps[4]).toEqual({ kind: "notice", recorded_at: "2024-01-02T03:00:07Z", text: "no flow logs" });
  });

  it("counts an ok search whose only row is an error as failed, and clamps its args", () => {
    const replay = rootCauseReplay(RUN, [
      search("x".repeat(600), found([{ error: "bad spl", query: "q" }])),
    ]);
    const only = replay.steps[0] as { failed: boolean; failure?: string; args: string };
    expect(only.failed).toBe(true);
    expect(only).not.toHaveProperty("failure");
    expect(only.args).toHaveLength(501);
  });

  it("leaves budgets out of an old ledger, and skips dispatches that are not searches", () => {
    const replay = rootCauseReplay(RUN, [event("run", { run_kind: "root_cause" }), search("{}", found([]), "record")]);
    expect(replay).not.toHaveProperty("budgets");
    expect(replay.steps).toEqual([]);
  });

  it("hides a name on an unproven step in every entry and shows it on a proven one", () => {
    const replay = rootCauseReplay(RUN, [
      event("step", step({ step_id: "step-1", who: "alice", event: "alice opened the file", link_status: "unproven" })),
      event("step", step({ step_id: "step-2", who: "bob", cause_id: "step-1", link: "a.exe", link_status: "proven" })),
      // alice's step is rewritten later, still unproven: the earlier frame must not show her either
      event("step", step({ step_id: "step-1", who: "alice", event: "alice opened the file", link_status: "unproven" })),
    ]);
    const text = replay.steps.map((s) => (s.kind === "step" ? [s.who, s.event] : [])).flat();
    expect(text).toEqual(["[unlinked]", "[unlinked] opened the file", "bob", "beacon to 203.0.113.5", "[unlinked]", "[unlinked] opened the file"]);
  });
});
