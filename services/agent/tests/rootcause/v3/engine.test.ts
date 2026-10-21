// The v3 loop as a worker run: what it journals and how it ends, on the real kernel with a scripted model.
import { describe, expect, it } from "vitest";
import type { AgentEvent, CallPayload } from "../../../contracts/events.js";
import { seedFrom } from "../../../core/budget.js";
import { InProcessState } from "../../../core/state.js";
import { conclusionOf, runRootCauseV3 } from "../../../plugins/rootcause/v3/engine.js";
import { engineOf, type V3Kinds } from "../../../plugins/rootcause/v3/ledger.js";
import { rootCauseV3Projection, rootCauseV3Replay } from "../../../plugins/rootcause/v3/projection.js";
import { SUBMIT_TOOL } from "../../../core/stream.js";
import { framed, harnessOf, modelOf, properties, specOf, splunk, telemetry } from "../../support/v3-run.js";

// The ledger without the model's streamed text, which is journaled as it arrives.
const kinds = (events: readonly AgentEvent<V3Kinds>[]) => events.map((e) => e.kind).filter((kind) => kind !== "content");
const fail = () => { throw new Error("the model was not meant to be called"); };

describe("runRootCauseV3", () => {
  it("opens its own ledger, stamped so every reader knows which engine wrote it", async () => {
    const state = new InProcessState<V3Kinds>();
    const spec = specOf({ tools: [] });
    await runRootCauseV3(harnessOf(spec, modelOf(fail), splunk(), state), { run_id: "r1", spec });
    const events = await state.read("r1");
    expect(events[0]!.kind).toBe("run");
    expect(engineOf(events)).toBe("rootcause-v3");
  });

  it("refuses a run bound to telemetry no adapter reads, naming the binding", async () => {
    const state = new InProcessState<V3Kinds>();
    const spec = specOf({ tools: [{ ...telemetry, id: "elastic_elastic_search_logs" }] });
    const done = await runRootCauseV3(harnessOf(spec, modelOf(fail), splunk(), state), { run_id: "r1", spec });
    expect(done.status).toBe("failed");
    expect(await state.terminal("r1")).toMatchObject({ outcome: "failed", reason: expect.stringContaining("elastic_elastic_search_logs") });
  });

  it("refuses a brief that says when nothing happened, rather than search blind; the framing turn is journaled and billed", async () => {
    const state = new InProcessState<V3Kinds>();
    const spec = specOf();
    const model = modelOf(() => ({ emit: framed({ observed_at: null }) }));
    const done = await runRootCauseV3(harnessOf(spec, model, splunk(), state), { run_id: "r1", spec });
    expect(done.status).toBe("failed");
    expect((await state.terminal("r1"))!.reason).toMatch(/no point in time/);
    expect(model.requests).toHaveLength(1);
    expect(model.requests[0]!.tools.map((tool) => tool.id)).toEqual([SUBMIT_TOOL]);
    expect(properties(model.requests[0]!)).toContain("observed_at");
    expect(model.requests[0]!.messages.map((m) => m.content).join("\n")).toContain("lab");
    expect(kinds(await state.read("r1"))).toEqual(["run", "frame", "spend", "terminal"]);
  });

  it("refuses when neither the brief nor the settings name an index", async () => {
    const state = new InProcessState<V3Kinds>();
    const spec = specOf();
    const done = await runRootCauseV3(harnessOf(spec, modelOf(() => ({ emit: framed({ index: null }) })), splunk(), state), { run_id: "r1", spec });
    expect(done.status).toBe("failed");
    expect((await state.terminal("r1"))!.reason).toMatch(/which of the indexes .*lab.*root_cause_index/);
  });

  it("ends budget_exhausted when the pool refuses the first model call", async () => {
    const state = new InProcessState<V3Kinds>();
    const spec = specOf({ budgets: { max_calls: 0, max_cost_usd: 10, max_wall_ms: 600_000, max_park_ms: 60_000 } });
    const done = await runRootCauseV3(harnessOf(spec, modelOf(fail), splunk(), state), { run_id: "r1", spec });
    expect(done.status).toBe("budget_exhausted");
    expect((await state.read("r1")).some((e) => e.kind === "spend")).toBe(false);
  });

  it("writes no terminal when the lease is already gone: the run is someone else's to resume", async () => {
    const state = new InProcessState<V3Kinds>();
    const spec = specOf();
    const lease = new AbortController();
    lease.abort(new Error("lost the lease"));
    const done = await runRootCauseV3(harnessOf(spec, modelOf(fail), splunk(), state), { run_id: "r1", spec, signal: lease.signal });
    expect(done.status).toBe("lost");
    expect(await state.terminal("r1")).toBeNull();
  });

  it("writes no terminal when the lease goes mid-run", async () => {
    const state = new InProcessState<V3Kinds>();
    const spec = specOf();
    const lease = new AbortController();
    const model = modelOf((request) => {
      if (properties(request).includes("observed_at")) { lease.abort(new Error("lost the lease")); return { emit: framed() }; }
      return {};
    });
    const done = await runRootCauseV3(harnessOf(spec, model, splunk(), state), { run_id: "r1", spec, signal: lease.signal });
    expect(done.status).toBe("lost");
    expect(await state.terminal("r1")).toBeNull();
  });

  it("an operator's stop ends the run aborted with the report so far, its searches journaled as calls", async () => {
    const state = new InProcessState<V3Kinds>();
    const spec = specOf();
    let searched = false;
    const model = modelOf((request) => {
      if (properties(request).includes("observed_at")) return { emit: framed() };
      if (properties(request).includes("map")) return { emit: { map: "One source, edr.", reasoning: "mapped" } };
      // The first turn names the move; the move then runs one search and answers. The stop arrives while the model writes it.
      if (properties(request).includes("why")) return { emit: { move: "strengthen", target: "N1", why: "the alert is unconfirmed" } };
      if (request.emit !== undefined) { searched = true; return { emit: { evidence: [], reasoning: "nothing found" }, wait: 1_500 }; }
      const moving = request.messages.some((m) => m.content.includes("<graph_snapshot>"));
      const searchedAlready = request.messages.some((m) => m.role === "tool");
      return moving && !searchedAlready
        ? { calls: [{ tool: "splunk_query", args: { purpose: "look for the process on ws-7", spl: "index=lab sourcetype=edr host=ws-7 | stats count by process" } }] }
        : { content: "done" };
    });
    const stop = async () => (searched ? { directive_id: "d1", actor: "ana", kind: "abort", text: "enough", created_at: "2026-01-05T00:00:00Z" } : null);
    const done = await runRootCauseV3(harnessOf(spec, model, splunk(), state), { run_id: "r1", spec, stop });
    expect(done).toMatchObject({ status: "aborted", reason: "an operator stopped the run: enough" });

    const events = await state.read("r1");
    expect(await state.terminal("r1")).toMatchObject({ outcome: "aborted", summary: expect.any(String) });
    expect(kinds(events)).toEqual(expect.arrayContaining(["rootcause_setup", "rootcause_turn", "rootcause_exit", "directive", "terminal"]));
    const exit = events.find((e) => e.kind === "rootcause_exit")!.payload as { exit: unknown; report: string };
    expect(exit.exit).toBeNull();
    expect(exit.report).toContain("# RCA report");

    // The first turn is a choose turn: journaled as a decision, its answer a call-free kernel turn.
    const decisions = events.filter((e) => e.kind === "rootcause_turn").flatMap((e) => (e.payload as { entries: { type: string; payload: unknown }[] }).entries).filter((x) => x.type === "decision").map((x) => x.payload);
    expect(decisions[0]).toMatchObject({ move: "choose" });
    expect(decisions[1]).toMatchObject({ move: "strengthen", targetId: "N1" });

    // The model's search is a call on the run's ledger, under the move's role, with the kernel's ref.
    const calls = events.filter((e) => e.kind === "call").map((e) => e.payload as CallPayload);
    const mine = calls.filter((c) => c.tool === "splunk_query" && c.role === "strengthen");
    expect(mine).toHaveLength(1);
    expect(mine[0]).toMatchObject({ ref: expect.stringMatching(/^r\d+$/), ok: true });
    expect(new Set(calls.map((c) => c.ref)).size).toBe(calls.length);

    // Both readers pick v3's shape off the engine stamp.
    const view = rootCauseV3Projection("r1", events as never);
    expect(view).toMatchObject({ engine: "rootcause-v3", outcome: "aborted" });
    // History counts the model's searches that ran; the code's own are counted apart.
    expect(view.searches).toBe(calls.filter((c) => c.role !== "code" && c.failure !== "refused").length);
    expect(view.code_searches).toBe(calls.filter((c) => c.role === "code").length);
    expect(rootCauseV3Replay("r1", events as never)).toMatchObject({ engine: "rootcause-v3" });
  }, 20_000);
});

describe("a run the pool cuts off mid-move", () => {
  it("records budget-exhausted, not null, in the ledger, the report and the projection", async () => {
    const state = new InProcessState<V3Kinds>();
    // Framing, mapping and the choose turn fit; the move's own call is refused.
    const spec = specOf({ budgets: { max_calls: 4, max_cost_usd: 10, max_wall_ms: 600_000, max_park_ms: 60_000 } });
    const model = modelOf((request) => {
      if (properties(request).includes("observed_at")) return { emit: framed() };
      if (properties(request).includes("map")) return { emit: { map: "One source, edr.", reasoning: "mapped" } };
      if (properties(request).includes("why")) return { emit: { move: "strengthen", target: "N1", why: "the alert is unconfirmed" } };
      return { emit: { evidence: [], reasoning: "nothing found" } };
    });
    const done = await runRootCauseV3(harnessOf(spec, model, splunk(), state), { run_id: "r1", spec });
    expect(done.status).toBe("budget_exhausted");
    const events = await state.read("r1");
    const exit = events.find((e) => e.kind === "rootcause_exit")!.payload as { exit: unknown; report: string };
    expect(exit.exit).toBe("budget-exhausted");
    expect(exit.report).toContain("exit **budget-exhausted**");
    expect(rootCauseV3Projection("r1", events as never)).toMatchObject({ exit: "budget-exhausted", outcome: "budget_exhausted" });
  }, 20_000);
});

describe("a resumed run", () => {
  it("continues at the turn after the last journaled one, with its setup, spend and calls as they were", async () => {
    const state = new InProcessState<V3Kinds>();
    const spec = specOf();
    const answer = (stop: () => void) => (request: Parameters<Parameters<typeof modelOf>[0]>[0]) => {
      if (properties(request).includes("observed_at")) return { emit: framed() };
      if (properties(request).includes("map")) return { emit: { map: "One source, edr.", reasoning: "mapped" } };
      if (request.emit !== undefined) { stop(); return { emit: { evidence: [], reasoning: "nothing found" }, wait: 1_500 }; }
      return { content: "done" };
    };

    // The first worker loses its lease while the first move is being answered.
    const lease = new AbortController();
    const first = modelOf(answer(() => lease.abort(new Error("lost the lease"))));
    expect((await runRootCauseV3(harnessOf(spec, first, splunk(), state), { run_id: "r1", spec, signal: lease.signal })).status).toBe("lost");
    expect(await state.terminal("r1")).toBeNull();
    const before = await state.read("r1");
    expect(kinds(before)).toEqual(expect.arrayContaining(["rootcause_setup", "rootcause_turn"]));
    const turnsOf = (events: typeof before) => events.filter((e) => e.kind === "rootcause_turn").map((e) => (e.payload as { turn: number }).turn);

    // The second picks it up: no framing, no mapping, and the next move starts where the first stopped.
    let stopped = false;
    const second = modelOf(answer(() => { stopped = true; }));
    const stop = async () => (stopped ? { directive_id: "d1", actor: "ana", kind: "abort", text: "", created_at: "2026-01-05T00:00:00Z" } : null);
    const done = await runRootCauseV3(harnessOf(spec, second, splunk(), state, seedFrom(before)), { run_id: "r1", spec, stop });
    expect(done.status).toBe("aborted");
    expect(second.requests.some((r) => properties(r).includes("observed_at") || properties(r).includes("map"))).toBe(false);
    const after = await state.read("r1");
    expect(after.filter((e) => e.kind === "rootcause_setup")).toHaveLength(1);
    expect(after.filter((e) => e.kind === "run")).toHaveLength(1);
    // Turns 0 and 1 were the first worker's; the second journals turn 2 and no turn twice.
    expect(turnsOf(before)).toEqual([0, 1]);
    expect(turnsOf(after)).toEqual([0, 1, 2]);
    const refs = after.filter((e) => e.kind === "call").map((e) => (e.payload as CallPayload).ref);
    expect(new Set(refs).size).toBe(refs.length);
  }, 20_000);
});

describe("a ledger the engine did not open", () => {
  it("is failed, not continued: v3 would write its events onto the kernel plugin's run", async () => {
    const state = new InProcessState<V3Kinds>();
    const spec = specOf();
    await state.append("r1", [{ run_id: "r1", run_kind: "root_cause", kind: "run", payload: { run_kind: "root_cause", spec, budgets: spec.budgets, seed: "r1", tenant_id: null, started_by: "x" } } as never]);
    const done = await runRootCauseV3(harnessOf(spec, modelOf(fail), splunk(), state), { run_id: "r1", spec });
    expect(done.status).toBe("failed");
    expect(kinds(await state.read("r1"))).toEqual(["run", "terminal"]);
  });
});

describe("conclusionOf", () => {
  it("is the investigator's conclusion section, else the report's opening", () => {
    expect(conclusionOf("# Report\n\n## Investigator's conclusion\n\nA mail did it.\n\n## 1. Verdict\n\nTP")).toBe("A mail did it.");
    expect(conclusionOf("# Report\n\nno conclusion")).toBe("# Report\n\nno conclusion");
  });
});
