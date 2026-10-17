// The v3 loop as a worker run: what it journals and how it ends when it cannot start.
import { describe, expect, it } from "vitest";
import type { AgentEvent } from "../../../contracts/events.js";
import type { ToolResult } from "../../../contracts/tool.js";
import { budgetOf, unmeteredQuota } from "../../../core/budget.js";
import type { Harness } from "../../../core/loop.js";
import { nullMemory } from "../../../core/memory.js";
import { registryOf } from "../../../core/registry.js";
import type { ToolDispatch } from "../../../core/seams.js";
import type { RunSpec } from "../../../core/spec.js";
import { InProcessState } from "../../../core/state.js";
import { conclusionOf, runRootCauseV3 } from "../../../plugins/rootcause/v3/engine.js";
import { engineOf, type V3Kinds } from "../../../plugins/rootcause/v3/ledger.js";
import type { Message, MessageRequest, Messages } from "../../../plugins/rootcause/v3/model/messages.js";
import { SPLUNK_EXECUTE } from "../../../plugins/rootcause/v3/tools/splunk-source.js";

const telemetry = { id: SPLUNK_EXECUTE, kind: "remote", description: "Run a Splunk search.", parameters: { type: "object" } };

function specOf(over: Partial<RunSpec> = {}): RunSpec {
  return {
    model: "claude-test",
    budgets: { max_calls: 50, max_cost_usd: 10, max_wall_ms: 600_000, max_park_ms: 60_000 },
    tools: [telemetry],
    sections: {},
    prompt: "A confirmed compromise: host ws-7 beaconed to 203.0.113.9.",
    ...over,
  } as RunSpec;
}

function harnessOf(spec: RunSpec, dispatch: ToolDispatch, state = new InProcessState<V3Kinds>()): Harness<V3Kinds> {
  return {
    provider: undefined as never,
    registry: registryOf([], {}),
    dispatch,
    budget: budgetOf(spec.budgets, unmeteredQuota),
    memory: nullMemory,
    state,
  };
}

// splunk_execute as the tools router answers: one envelope row.
const indexes: ToolDispatch = {
  invoke: async (): Promise<ToolResult> => ({ ok: true, rows: [{ success: true, count: 1, results: [{ index: "lab", count: "1200" }] }], rowCount: 1, capped: false, sourceSystem: "splunk-selfhosted" }),
};

// A model that answers every call by stating the given alert.
function framing(input: Record<string, unknown>, asked: MessageRequest[] = []): Messages {
  return {
    create: async (request): Promise<Message> => {
      asked.push(request);
      return { content: [{ type: "tool_use", id: "t1", name: "frame_alert", input }], stop_reason: "tool_use", usage: { input_tokens: 900, output_tokens: 40 } };
    },
  };
}

const kinds = (events: readonly AgentEvent<V3Kinds>[]) => events.map((e) => e.kind);

describe("runRootCauseV3", () => {
  it("opens its own ledger, stamped so every reader knows which engine wrote it", async () => {
    const state = new InProcessState<V3Kinds>();
    const spec = specOf({ tools: [] });
    await runRootCauseV3(harnessOf(spec, indexes, state), { run_id: "r1", spec, messages: () => framing({}) });
    const events = await state.read("r1");
    expect(events[0]!.kind).toBe("run");
    expect(engineOf(events)).toBe("rootcause-v3");
  });

  it("refuses a run bound to telemetry no adapter reads, naming the binding", async () => {
    const state = new InProcessState<V3Kinds>();
    const spec = specOf({ tools: [{ ...telemetry, id: "elastic_elastic_search_logs" }] });
    const done = await runRootCauseV3(harnessOf(spec, indexes, state), { run_id: "r1", spec, messages: () => framing({}) });
    expect(done.status).toBe("failed");
    expect(await state.terminal("r1")).toMatchObject({ outcome: "failed", reason: expect.stringContaining("elastic_elastic_search_logs") });
  });

  it("refuses a brief that says when nothing happened, rather than search blind; the framing call is billed", async () => {
    const state = new InProcessState<V3Kinds>();
    const spec = specOf();
    const asked: MessageRequest[] = [];
    const done = await runRootCauseV3(harnessOf(spec, indexes, state), {
      run_id: "r1", spec,
      messages: () => framing({ title: "beacon", observed_at: null, source: "network", artifacts: [{ kind: "host", value: "ws-7" }], index: "lab" }, asked),
    });
    expect(done.status).toBe("failed");
    expect((await state.terminal("r1"))!.reason).toMatch(/no point in time/);
    expect(asked[0]!.messages[0]!.content).toContain("lab");
    expect(asked[0]!.tool_choice).toEqual({ type: "tool", name: "frame_alert" });
    expect(kinds(await state.read("r1"))).toEqual(["run", "call", "spend", "terminal"]);
  });

  it("refuses when neither the brief nor the settings name an index", async () => {
    const state = new InProcessState<V3Kinds>();
    const spec = specOf();
    const done = await runRootCauseV3(harnessOf(spec, indexes, state), {
      run_id: "r1", spec,
      messages: () => framing({ title: "beacon", observed_at: "2026-01-05T10:00:00Z", source: "network", artifacts: [], index: null }),
    });
    expect(done.status).toBe("failed");
    expect((await state.terminal("r1"))!.reason).toMatch(/which of the indexes .*lab.*root_cause_index/);
  });

  it("ends budget_exhausted when the pool refuses the first model call", async () => {
    const state = new InProcessState<V3Kinds>();
    const spec = specOf({ budgets: { max_calls: 0, max_cost_usd: 10, max_wall_ms: 600_000, max_park_ms: 60_000 } });
    const done = await runRootCauseV3(harnessOf(spec, indexes, state), { run_id: "r1", spec, messages: () => framing({}) });
    expect(done.status).toBe("budget_exhausted");
    expect((await state.read("r1")).some((e) => e.kind === "spend")).toBe(false);
  });

  it("writes no terminal when the lease is already gone: the run is someone else's to resume", async () => {
    const state = new InProcessState<V3Kinds>();
    const spec = specOf();
    const lease = new AbortController();
    lease.abort(new Error("lost the lease"));
    const done = await runRootCauseV3(harnessOf(spec, indexes, state), { run_id: "r1", spec, signal: lease.signal, messages: () => framing({}) });
    expect(done.status).toBe("lost");
    expect(await state.terminal("r1")).toBeNull();
  });
});

describe("conclusionOf", () => {
  it("is the investigator's conclusion section, else the report's opening", () => {
    expect(conclusionOf("# Report\n\n## Investigator's conclusion\n\nA mail did it.\n\n## 1. Verdict\n\nTP")).toBe("A mail did it.");
    expect(conclusionOf("# Report\n\nno conclusion")).toBe("# Report\n\nno conclusion");
  });
});
