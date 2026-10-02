import { describe, expect, it } from "vitest";
import { archFor } from "../../arch/registry.js";
import type { AgentEvent } from "../../contracts/events.js";
import { PROVER_TOOL, type RootCauseKinds, type StepPayload } from "../../workflows/rootcause/proof.js";
import { RECENT_SEARCHES, rootCauseProjection } from "../../workflows/rootcause/projection.js";

const RUN = "5a2c2d3e-0000-4000-8000-000000000c39";
const AT = "2024-01-02T03:00:00Z";

let seq = 0;
function event(kind: string, payload: unknown): AgentEvent<RootCauseKinds> {
  seq += 1;
  return { run_id: RUN, run_kind: "root_cause", seq, ts: AT, kind, payload } as unknown as AgentEvent<RootCauseKinds>;
}

function step(over: Partial<StepPayload>): StepPayload {
  return {
    step_id: "step-1",
    event: "beacon to 203.0.113.5",
    who: "",
    at: AT,
    link: "",
    artifact: "",
    cause_id: null,
    origin: false,
    link_status: "none",
    origin_status: "none",
    ...over,
  };
}

function search(args: string, rows: unknown[], ok = true): AgentEvent<RootCauseKinds> {
  return event("dispatch", {
    dispatch_id: `dsp-${seq}`,
    agent_id: "lead",
    status: ok ? "complete" : "failed",
    question_id: null,
    failure_reason: ok ? null : "unavailable",
    result: ok
      ? { ok: true, rows, rowCount: rows.length, capped: false, sourceSystem: "splunk" }
      : { ok: false, failure: { kind: "unavailable", detail: "splunk is down" } },
    calls: [{ tool: PROVER_TOOL, arguments: args }],
  });
}

function spend(cost: number | null): AgentEvent<RootCauseKinds> {
  return event("spend", { role: "lead", tokens: {}, cost_usd: cost });
}

describe("the root-cause projection", () => {
  it("is what serve hands a reader for a root_cause run", () => {
    expect(archFor("root_cause").projection).toBeTypeOf("function");
  });

  it("reports a running trace: the latest write of each step, its proof, the spend so far", () => {
    const folded = rootCauseProjection(RUN, [
      event("run", { run_kind: "root_cause", budgets: { max_calls: 1024, max_cost_usd: 15, max_wall_ms: 5_400_000 } }),
      spend(0.01),
      search('{"spl_query":"index=botsv3 \\"invoice.lnk\\""}', [{ success: true, query: "q", count: 2, results: [{ file: "invoice.lnk" }, { file: "invoice.lnk" }] }]),
      event("step", step({ link_status: "unproven" })),
      spend(0.02),
      event("step", step({ origin: true, origin_status: "proven" })),
      event("step", step({ step_id: "step-2", cause_id: "step-1", link: "invoice.lnk", link_status: "unproven" })),
      event("notice", { text: "the bound telemetry cannot prove a link" }),
      spend(null),
    ]);
    expect(folded).toMatchObject({
      run_id: RUN,
      run_kind: "root_cause",
      status: "running",
      outcome: null,
      searches: 1,
      notices: ["the bound telemetry cannot prove a link"],
    });
    expect(folded.cost_usd).toBeCloseTo(0.03);
    expect(folded.max_cost_usd).toBe(15);
    expect(folded.steps.map((s) => [s.step_id, s.proven])).toEqual([
      ["step-1", true],
      ["step-2", false],
    ]);
    expect(folded.proven).toBe(1);
    expect(folded.recent_searches[0]).toMatchObject({ tool: PROVER_TOOL, rows: 2, failed: false });
  });

  it("names a who only on a proven step, as the report does", () => {
    const folded = rootCauseProjection(RUN, [
      event("step", step({ who: "alice", event: "alice opened invoice.lnk", origin: true, origin_status: "proven" })),
      event("step", step({ step_id: "step-2", who: "mallory", event: "mallory was on the host", cause_id: "step-1", link_status: "unproven" })),
    ]);
    expect(folded.steps.map((s) => [s.who, s.event])).toEqual([
      ["alice", "alice opened invoice.lnk"],
      ["[unlinked]", "[unlinked] was on the host"],
    ]);
  });

  it("leaves the cost null until something is priced", () => {
    const folded = rootCauseProjection(RUN, [event("run", {}), spend(null)]);
    expect(folded.cost_usd).toBeNull();
    expect(folded.max_cost_usd).toBeNull();
  });

  it("marks a search that failed, either as a failed result or as an error row", () => {
    const folded = rootCauseProjection(RUN, [
      search("| makeresults", [{ error: "Splunk search failed or timed out", query: "| makeresults" }]),
      search("| makeresults", [], false),
    ]);
    expect(folded.recent_searches.map((s) => [s.rows, s.failed])).toEqual([
      [0, true],
      [0, true],
    ]);
  });

  it("keeps only the latest searches and clips long arguments, while counting all of them", () => {
    const many = Array.from({ length: RECENT_SEARCHES + 5 }, (_, at) => search(`search ${at} ${"x".repeat(2_000)}`, []));
    const folded = rootCauseProjection(RUN, many);
    expect(folded.searches).toBe(RECENT_SEARCHES + 5);
    expect(folded.recent_searches).toHaveLength(RECENT_SEARCHES);
    expect(folded.recent_searches.at(-1)?.args.startsWith(`search ${RECENT_SEARCHES + 4} `)).toBe(true);
    expect(folded.recent_searches[0]!.args.length).toBeLessThan(600);
  });

  it("reports an open permit, then the terminal once there is one", () => {
    const permit = event("checkpoint", { checkpoint_id: "cp-root-cause-permit", checkpoint_class: "hypothesis_approval", question: "Permit?", raised_at: AT });
    expect(rootCauseProjection(RUN, [permit]).status).toBe("waiting_approval");
    expect(rootCauseProjection(RUN, [permit]).open_checkpoint?.checkpoint_id).toBe("cp-root-cause-permit");
    const ended = rootCauseProjection(RUN, [
      permit,
      event("resolution", { checkpoint_id: "cp-root-cause-permit", actor: "sam", answer: "approve", text: "", resolved_at: AT }),
      event("terminal", { outcome: "completed", reason: "the trace finished", summary: "done" }),
    ]);
    expect(ended).toMatchObject({ status: "terminal", outcome: "completed", reason: "the trace finished", open_checkpoint: null });
  });
});
