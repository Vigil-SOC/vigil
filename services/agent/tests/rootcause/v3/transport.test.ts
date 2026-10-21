// A move is one kernel turn: its role, its schema, its searches as calls on the run's ledger, its answer checked once.
import { describe, expect, it } from "vitest";
import type { CallPayload } from "../../../contracts/events.js";
import { InProcessState } from "../../../core/state.js";
import { loadPlugin } from "../../../plugins/index.js";
import { defaultConfig } from "../../../plugins/rootcause/v3/config.js";
import type { V3Kinds } from "../../../plugins/rootcause/v3/ledger.js";
import { KernelModelClient, KernelTurns, Refused, type TurnRequest } from "../../../plugins/rootcause/v3/model/client.js";
import { constraints, SYSTEM_PROMPT } from "../../../plugins/rootcause/v3/model/prompts.js";
import { issued, neverRan, Registry, type EvidenceTool } from "../../../plugins/rootcause/v3/tools/registry.js";
import { MemoryTrace } from "../../../plugins/rootcause/v3/trace.js";
import { toolsFrom } from "../../../tools/remote.js";
import { harnessOf, modelOf, specOf, splunk } from "../../support/v3-run.js";

const cfg = { ...defaultConfig };
const range: [string, string] = ["2026-01-05T00:00:00Z", "2026-01-06T00:00:00Z"];

// An evidence tool that answers from a table: what the model's search comes back with.
const evidence = (answers: Record<string, string>): EvidenceTool => ({
  name: "splunk_query",
  description: "Search.",
  inputSchema: { type: "object", properties: { purpose: { type: "string" }, spl: { type: "string" } }, required: ["purpose", "spl"] },
  readOnly: true,
  run: async (input) => (input.spl in answers ? { ok: true, data: answers[input.spl]! } : { ok: false, error: `refused: no such search ${input.spl}` }),
});

const cand = (label: string, ids: string[]) => ({ label, domain: "edr", prior: 0.3, artifacts: [], nodeEvidence: [{ summary: "s", strength: "direct", polarity: "supports", toolCallIds: ids }], edgeEvidence: [] });

function setup(answer: Parameters<typeof modelOf>[0], table: Record<string, string> = {}, spec = specOf()) {
  const state = new InProcessState<V3Kinds>();
  const provider = modelOf(answer);
  const harness = harnessOf(spec, provider, splunk(), state);
  const turns = new KernelTurns(harness as never, { run_id: "r1", run_kind: "root_cause", spec, plugin: loadPlugin("rootcause").manifest, tools: toolsFrom(spec.tools) });
  const tools = new Registry().register(evidence(table));
  const client = new KernelModelClient({ cfg, tools, domains: ["edr"], survey: "## Survey\n- edr: 10 events", range }, turns);
  const trace = new MemoryTrace("r1");
  const request = (over: Partial<TurnRequest> = {}): TurnRequest => ({ move: "expand", correlationId: "N1", userContent: "Find the cause.", submitTool: "submit_causes", allowedDomains: ["edr"], trace: trace.forTurn(1, "expand", "N1"), ...over });
  const calls = async () => (await state.read("r1")).filter((e) => e.kind === "call").map((e) => e.payload as CallPayload);
  return { state, provider, tools, client, trace, request, calls };
}

const SEARCH = { purpose: "find the process on ws-7", spl: "index=lab sourcetype=edr host=ws-7 | stats count by process" };

describe("a move as a kernel turn", () => {
  it("runs under the move's role and schema with the evidence tool, journaling each search as a call the answer can cite", async () => {
    const t = setup(
      (request) => {
        if (request.emit !== undefined) return { emit: { candidates: [cand("curl wrote /tmp/x", ["r1"]), cand("ghost event", ["r9"])], reasoning: "r" } };
        return request.messages.some((m) => m.role === "tool") ? { content: "done" } : { calls: [{ tool: "splunk_query", args: SEARCH }] };
      },
      { [SEARCH.spl]: "3 events matched\ncurl wrote /tmp/x" },
    );
    t.client.addContext("## Environment\none source");
    const out = await t.client.runTurn(t.request());

    // The kernel's turn: system, the move's task, the evidence tool only, the move's schema for the emission.
    const [first, , emission] = t.provider.requests;
    expect(first!.messages[0]!.content).toContain(SYSTEM_PROMPT.slice(0, 80));
    expect(first!.messages[0]!.content).toContain("- edr: 10 events");
    expect(first!.messages[0]!.content).toContain("## Environment");
    expect(first!.messages.some((m) => m.content.includes("Find the cause."))).toBe(true);
    expect(first!.tools.map((x) => x.id)).toEqual(["splunk_query"]);
    expect(Object.keys((emission!.emit!["properties"] ?? {}) as object)).toContain("candidates");
    expect(JSON.stringify(emission!.emit)).not.toContain("maxLength");

    // The search is a call on the run's ledger under the move's role, and the model was shown its ref.
    const calls = await t.calls();
    expect(calls).toHaveLength(1);
    expect(calls[0]).toMatchObject({ ref: "r1", role: "expand", tool: "splunk_query", ok: true });
    const shown = t.provider.requests[1]!.messages.find((m) => m.role === "tool")!.content;
    expect(shown).toContain('ref="r1"');
    expect(shown).toContain("curl wrote /tmp/x");
    expect(out.toolCalls).toMatchObject([{ id: "r1", ok: true, text: "3 events matched\ncurl wrote /tmp/x" }]);
    expect(t.tools.callCount).toBe(1);
    expect(t.tools.inputOf("r1")).toEqual(SEARCH);
    expect(t.tools.outputOf("r1")).toBe("3 events matched\ncurl wrote /tmp/x");
    expect(t.trace.chronological().filter((e) => e.type === "query").map((e) => (e.payload as { toolCallId: string }).toolCallId)).toEqual(["r1"]);

    // The emitted value is checked once: the item citing a ref the kernel never journaled is dropped, the rest kept.
    expect((out.submission as { candidates: { label: string }[] }).candidates.map((c) => c.label)).toEqual(["curl wrote /tmp/x"]);
    expect(t.trace.chronological().some((e) => e.type === "rejected-submission" && JSON.stringify(e.payload).includes("r9"))).toBe(true);
  });

  it("a move with no searches asks nothing of the tool, and a failed search comes back as a failed call", async () => {
    const t = setup(
      (request) => (request.emit !== undefined ? { emit: { candidates: [], reasoning: "r" } } : request.messages.some((m) => m.role === "tool") ? { content: "done" } : { calls: [{ tool: "splunk_query", args: { ...SEARCH, spl: "index=lab nope" } }] }),
    );
    const searched = await t.client.runTurn(t.request());
    expect(searched.toolCalls).toMatchObject([{ id: "r1", ok: false }]);
    expect((await t.calls())[0]).toMatchObject({ ok: false, failure: "backend_error" });
    expect(t.tools.outputOf("r1")).toBe("");
    // It never ran: it keeps its number but is no evidence, and the next search is r2.
    expect(t.tools.callCount).toBe(1);
    expect(issued(t.tools, "r1")).toBe(false);
    expect(t.tools.coverage).toEqual({});

    const none = await t.client.runTurn(t.request({ move: "conclude", submitTool: "submit_conclusion", maxSearches: 0 }));
    expect(t.provider.requests.at(-1)!.tools).toEqual([]);
    expect(none.toolCalls).toEqual([]);
  });

  it("holds a search to the tool's schema: one without a purpose comes back as invalid_args and never reaches the backend", async () => {
    const t = setup(
      (request) => (request.emit !== undefined ? { emit: { candidates: [], reasoning: "r" } } : request.messages.some((m) => m.role === "tool") ? { content: "done" } : { calls: [{ tool: "splunk_query", args: { spl: SEARCH.spl } }] }),
      { [SEARCH.spl]: "3 events matched" },
    );
    const out = await t.client.runTurn(t.request());
    expect(out.toolCalls).toMatchObject([{ id: "r1", ok: false }]);
    expect((await t.calls())[0]).toMatchObject({ ok: false, failure: "invalid_args" });
    expect(out.toolCalls[0]!.text).toMatch(/^bad input: \/ must have required property 'purpose'/);
  });

  it("trims an over-long next.why instead of rejecting the submission", async () => {
    const t = setup((request) => (request.emit !== undefined ? { emit: { candidates: [], reasoning: "r", next: { move: "expand", why: "x".repeat(400) } } } : { content: "done" }));
    const out = await t.client.runTurn(t.request());
    const why = (out.submission as { next: { why: string } }).next.why;
    expect(why).toHaveLength(300);
    expect(why.endsWith("…")).toBe(true);
  });

  it("tells the model the cap is rounds of searches, which may hold several", () => {
    expect(constraints(cfg, range, ["edr"], ["edr"])).toContain(`search rounds this turn: at most ${cfg.maxToolCallsPerTurn} (a round may hold several searches)`);
  });

  it("drops a citation of a refused call, live and as a resume rebuilds it", () => {
    const refused = { failure: "refused", result: "" };
    const guarded = { failure: "backend_error", result: '<vigil:tool_result tool="splunk_query" ref="r2">\nfailed: backend_error -- refused: name the index\n</vigil:tool_result>' };
    const ran = { failure: null, result: "<vigil:tool_result>\n1 row(s)\n</vigil:tool_result>" };
    const reg = new Registry();
    [refused, guarded, ran].forEach((c, i) => reg.note(`r${i + 1}`, { spl: "sourcetype=a" }, "", !neverRan(c)));
    expect(["r1", "r2", "r3"].map((id) => issued(reg, id))).toEqual([false, false, true]);
    expect(reg.callCount).toBe(3);
    expect(reg.coverage).toEqual({ a: 1 });
  });

  it("cites only refs as the ledger spells them", async () => {
    const { issued } = await import("../../../plugins/rootcause/v3/tools/registry.js");
    expect(["r1", "r3", "r03", "r0", "r4", "tc_1"].map((id) => issued({ callCount: 3 }, id))).toEqual([true, true, false, false, false, false]);
  });

  it("is one attempt on its target when the model never emits a valid submission", async () => {
    const t = setup((request) => (request.emit !== undefined ? { emit: { nothing: true } } : { content: "done" }));
    const out = await t.client.runTurn(t.request());
    expect(out.submission).toBeNull();
    expect(t.trace.chronological().some((e) => e.type === "rejected-submission")).toBe(true);
  });

  it("ends the run when the pool refuses another call", async () => {
    const spec = specOf({ budgets: { max_calls: 0, max_cost_usd: 10, max_wall_ms: 600_000, max_park_ms: 60_000 } });
    const t = setup(() => ({}), {}, spec);
    await expect(t.client.runTurn(t.request())).rejects.toBeInstanceOf(Refused);
  });
});
