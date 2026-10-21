// A move is one kernel turn: its role, its schema, its searches as calls on the run's ledger, its answer checked once.
import { describe, expect, it } from "vitest";
import type { CallPayload } from "../../../contracts/events.js";
import { InProcessState } from "../../../core/state.js";
import { loadPlugin } from "../../../plugins/index.js";
import { defaultConfig } from "../../../plugins/rootcause/v3/config.js";
import type { V3Kinds } from "../../../plugins/rootcause/v3/ledger.js";
import { KernelModelClient, KernelTurns, Refused, type TurnRequest } from "../../../plugins/rootcause/v3/model/client.js";
import { constraints, instructions, SYSTEM_PROMPT } from "../../../plugins/rootcause/v3/model/prompts.js";
import { issued, neverRan, Registry, type EvidenceTool } from "../../../plugins/rootcause/v3/tools/registry.js";
import { MemoryTrace } from "../../../plugins/rootcause/v3/trace.js";
import { toolsFrom } from "../../../tools/remote.js";
import { SUBMIT_TOOL } from "../../../core/stream.js";
import { harnessOf, modelOf, specOf, splunk } from "../../support/v3-run.js";
import { apply, node, seeded } from "./helpers.js";

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
    expect(first!.tools.map((x) => x.id)).toEqual(["splunk_query", SUBMIT_TOOL]);
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

  // A move that searches to its cap made one call per round and then one more for the emission.
  it("takes a move's answer on the round its search cap makes the last, with no further call", async () => {
    const t = setup(
      (request) => (request.force === SUBMIT_TOOL ? { emit: { candidates: [cand("curl wrote /tmp/x", ["r1"])], reasoning: "r" } } : { calls: [{ tool: "splunk_query", args: SEARCH }] }),
      { [SEARCH.spl]: "3 events matched\ncurl wrote /tmp/x" },
    );
    const out = await t.client.runTurn(t.request({ maxSearches: 2 }));

    expect(t.provider.requests.map((r) => r.force)).toEqual([undefined, SUBMIT_TOOL]);
    expect(t.provider.requests.some((r) => r.emit !== undefined)).toBe(false);
    expect(out.toolCalls).toMatchObject([{ id: "r1", ok: true }]);
    expect((out.submission as { candidates: { label: string }[] }).candidates.map((c) => c.label)).toEqual(["curl wrote /tmp/x"]);
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

  describe("an emission the kernel rejected", () => {
    const art = (kind: string) => ({ kind, value: "a@b.test" });
    const withArtifacts = (label: string, artifacts: unknown[]) => ({ ...cand(label, []), artifacts });
    const salvaged = (t: ReturnType<typeof setup>) => t.trace.chronological().some((e) => e.type === "rejected-submission" && /dropped 1 item/.test(JSON.stringify(e.payload)));

    it("keeps the sound candidates and drops the artifact that missed the enum, nested inside a candidate", async () => {
      const bad = { candidates: [withArtifacts("good", [art("email-address")]), withArtifacts("has a bad artifact", [art("host"), art("email-sender")]), cand("also good", [])], reasoning: "r" };
      const t = setup((request) => (request.emit !== undefined ? { emit: bad } : { content: "done" }));
      const out = await t.client.runTurn(t.request());

      expect(t.provider.requests.filter((r) => r.emit !== undefined)).toHaveLength(2);
      const got = (out.submission as { candidates: { label: string; artifacts: unknown[] }[] }).candidates;
      expect(got.map((c) => c.label)).toEqual(["good", "has a bad artifact", "also good"]);
      expect(got[1]!.artifacts).toEqual([art("host")]);
      expect(salvaged(t)).toBe(true);
    });

    it("drops a top-level item, and returns null when that leaves a list under its schema minimum", async () => {
      const one = { evidence: [{ summary: "s", strength: "direct", polarity: "supports", toolCallIds: [] }], artifacts: [art("email-sender"), art("host")], reasoning: "r" };
      const t = setup((request) => (request.emit !== undefined ? { emit: one } : { content: "done" }));
      const out = await t.client.runTurn(t.request({ submitTool: "submit_assessment" }));
      expect((out.submission as { artifacts: unknown[] }).artifacts).toEqual([art("host")]);

      const theory = (id: string) => ({ id, story: "s", accountsFor: [], gaps: "g", predictions: [{ id: "P1", sourcetype: "edr", expect: "e", spl: "index=lab", separates: [] }] });
      const two = { theories: [theory("H1"), { ...theory("H2"), predictions: [{ id: "P2", sourcetype: "edr", expect: 7, spl: "x", separates: [] }] }], nextTests: ["P1"], reasoning: "r" };
      const u = setup((request) => (request.emit !== undefined ? { emit: two } : { content: "done" }));
      expect((await u.client.runTurn(u.request({ submitTool: "submit_theories" }))).submission).toBeNull();
    });

    describe("a key the schema does not know", () => {
      const boundary = { targetDomain: "email", reason: "the mail gateway would record the sender", inDomainExhausted: true };
      const emit = async (submission: unknown, submitTool: TurnRequest["submitTool"] = "submit_causes") => {
        const t = setup((request) => (request.emit !== undefined ? { emit: submission } : { content: "done" }));
        const out = await t.client.runTurn(t.request({ submitTool }));
        return { t, got: out.submission as { candidates: { label: string }[]; boundary?: unknown } | null, note: t.trace.chronological().find((e) => e.type === "rejected-submission")?.payload as Record<string, string> | undefined };
      };

      it("is removed from a candidate that is kept, and a boundary in it moves up to the top level", async () => {
        const { got, note } = await emit({ candidates: [{ ...cand("phishing email delivered", []), boundary }, cand("also good", [])], reasoning: "r" });
        expect(got!.candidates.map((c) => c.label)).toEqual(["phishing email delivered", "also good"]);
        expect(got!.candidates[0]).not.toHaveProperty("boundary");
        expect(got!.boundary).toEqual(boundary);
        expect(note!["salvaged"]).toBe("kept the rest, dropped 0 item(s)");
        expect(note!["repaired"]).toBe("stripped 1 unknown key(s), lifted boundary to the top level");
      });

      it("is dropped, not lifted, when the top level already has that field or the value does not fit it", async () => {
        const top = { ...boundary, reason: "set at the top" };
        const held = await emit({ candidates: [{ ...cand("kept", []), boundary }], boundary: top, reasoning: "r" });
        expect(held.got!.candidates[0]).not.toHaveProperty("boundary");
        expect(held.got!.boundary).toEqual(top);
        expect(held.note!["repaired"]).toBe("stripped 1 unknown key(s)");
        const misfit = await emit({ candidates: [{ ...cand("kept", []), boundary: { targetDomain: "email" } }], reasoning: "r" });
        expect(misfit.got!.candidates.map((c) => c.label)).toEqual(["kept"]);
        expect(misfit.got).not.toHaveProperty("boundary");
        expect(misfit.note!["repaired"]).toBe("stripped 1 unknown key(s)");
      });

      it("is removed from the candidate, whatever it is called, and from the submission itself", async () => {
        const inner = await emit({ candidates: [{ ...cand("kept", []), notes: "x" }], reasoning: "r" });
        expect(inner.got!.candidates).toEqual([cand("kept", [])]);
        expect(inner.note!["repaired"]).toBe("stripped 1 unknown key(s)");
        const root = await emit({ candidates: [cand("kept", [])], reasoning: "r", confidence: 0.9 });
        expect(root.got).toEqual({ candidates: [cand("kept", [])], reasoning: "r" });
        expect(root.note!["repaired"]).toBe("stripped 1 unknown key(s)");
      });

      it("does not save a candidate with a real error: it is dropped, and what it carried is not lifted", async () => {
        const { label: _, ...unlabelled } = cand("", []);
        const { got, note } = await emit({ candidates: [{ ...unlabelled, boundary }, cand("kept", [])], reasoning: "r" });
        expect(got!.candidates.map((c) => c.label)).toEqual(["kept"]);
        expect(got).not.toHaveProperty("boundary");
        expect(note!["salvaged"]).toBe("kept the rest, dropped 1 item(s)");
        expect(note).not.toHaveProperty("repaired");
      });

      it("is lifted only from a candidate the check keeps, and only the dropped candidate is counted", async () => {
        const lost = await emit({ candidates: [{ ...cand("cites a search never run", ["r9"]), boundary }, { ...cand("kept", []), notes: "x" }], reasoning: "r" });
        expect(lost.got!.candidates.map((c) => c.label)).toEqual(["kept"]);
        expect(lost.got).not.toHaveProperty("boundary");
        expect(lost.note!["salvaged"]).toBe("kept the rest, dropped 1 item(s)");
        expect(lost.note!["repaired"]).toBe("stripped 2 unknown key(s)");
        const kept = await emit({ candidates: [cand("cites a search never run", ["r9"]), { ...cand("kept", []), boundary }], reasoning: "r" });
        expect(kept.got!.candidates).toEqual([cand("kept", [])]);
        expect(kept.got!.boundary).toEqual(boundary);
        expect(kept.note!["salvaged"]).toBe("kept the rest, dropped 1 item(s)");
        expect(kept.note!["repaired"]).toBe("stripped 1 unknown key(s), lifted boundary to the top level");
      });

      it("does not fill a missing required field from an item: the submission still fails", async () => {
        const ev = [{ summary: "s", strength: "direct", polarity: "supports", toolCallIds: [] }];
        expect((await emit({ causes: [{ ...cand("upstream", []), evidence: ev }], reasoning: "r" }, "submit_assessment")).got).toBeNull();
      });

      it("is dropped, not lifted, when the submission has no such field (entry points carry no boundary)", async () => {
        const { got, note } = await emit({ candidates: [{ ...cand("kept", []), boundary }], reasoning: "r" }, "submit_entry_points");
        expect(got).toEqual({ candidates: [cand("kept", [])], reasoning: "r" });
        expect(note!["repaired"]).toBe("stripped 1 unknown key(s)");
      });
    });

    it("gets nothing from an emission that was not JSON", async () => {
      const t = setup((request) => (request.emit !== undefined ? { emit: "no json here" } : { content: "done" }));
      expect((await t.client.runTurn(t.request())).submission).toBeNull();
    });

    it("accepts an email address and a message id as artifacts", async () => {
      const ok = { candidates: [withArtifacts("mail sent", [art("email-address"), { kind: "message-id", value: "<abc@mail.test>" }])], reasoning: "r" };
      const t = setup((request) => (request.emit !== undefined ? { emit: ok } : { content: "done" }));
      const out = await t.client.runTurn(t.request());
      expect(t.provider.requests.filter((r) => r.emit !== undefined)).toHaveLength(1);
      expect((out.submission as { candidates: { artifacts: unknown[] }[] }).candidates[0]!.artifacts).toHaveLength(2);
    });
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

  it("cites only refs as the ledger spells them", () => {
    expect(["r1", "r3", "r03", "r0", "r4", "tc_1"].map((id) => issued({ callCount: 3 }, id))).toEqual([true, true, false, false, false, false]);
  });

  it("is one attempt on its target when the model never emits a valid submission", async () => {
    const t = setup((request) => (request.emit !== undefined ? { emit: { nothing: true } } : { content: "done" }));
    const out = await t.client.runTurn(t.request());
    expect(out.submission).toBeNull();
    expect(t.trace.chronological().some((e) => e.type === "rejected-submission")).toBe(true);
  });

  it("reports a provider error on the emission or the tool turn as failed, apart from an emission the model got wrong", async () => {
    const down = () => { throw new Error("502 failed to execute HTTP request to provider API"); };
    for (const dies of ["emit", "tool"]) {
      const t = setup((request) => ((dies === "emit") === (request.emit !== undefined) ? down() : request.emit !== undefined ? { emit: { candidates: [], reasoning: "r" } } : { content: "done" }));
      const out = await t.client.runTurn(t.request());
      expect(out.submission).toBeNull();
      expect(out.failed).toContain("502");
      expect(t.trace.chronological().map((e) => e.type)).toContain("provider-failure");
      expect(t.trace.chronological().map((e) => e.type)).not.toContain("rejected-submission");
    }
    const bad = setup((request) => (request.emit !== undefined ? { emit: { nothing: true } } : { content: "done" }));
    expect((await bad.client.runTurn(bad.request())).failed).toBeUndefined();
  });

  it("ends the run when the pool refuses another call", async () => {
    const spec = specOf({ budgets: { max_calls: 0, max_cost_usd: 10, max_wall_ms: 600_000, max_park_ms: 60_000 } });
    const t = setup(() => ({}), {}, spec);
    await expect(t.client.runTurn(t.request())).rejects.toBeInstanceOf(Refused);
  });
});

describe("what a move's prompt tells the model to call", () => {
  // Every move's instructions, built on a graph that has the pieces each one names.
  const moves = (): Record<string, string> => {
    const g = seeded();
    apply(g, { addNodes: [node("m", { kind: "boundary-marker", originNodeId: "N1", targetDomain: "edr" }), node("leaf", { label: "leaf" })], addEdges: [{ tempId: "e", kind: "explains", source: "m", target: "N1", prior: 0.3, evidence: [] }] });
    const [leaf] = Object.keys(g.nodes).filter((id) => g.nodes[id]!.label === "leaf");
    g.theories = [{ id: "R1.H1", round: 1, story: "s", accountsFor: [], gaps: "", status: "live", support: 0, against: 0, predictions: [{ id: "R1.P1", sourcetype: "edr", expect: "x", spl: "x", separates: [], next: false, status: "open" }] }];
    return {
      strengthen: instructions.strengthen(g, "N1"), expand: instructions.expand(g, "N1"), fork: instructions.fork(g, "N1"), branch: instructions.branch(g, "N1"),
      pivotEntry: instructions.pivotEntry(g, "B1", range), pivotForward: instructions.pivotForward(g, "B1", leaf!), connect: instructions.connect(g, "N1", [leaf!]),
      scope: instructions.scope(), test: instructions.test(g, "R1.P1"), theorize: instructions.theorize(), choose: instructions.choose(), conclude: instructions.conclude("Enough."),
    };
  };

  it("names no submit tool: the answer is asked for in the kernel's emit step, which grants none", () => {
    for (const [move, text] of Object.entries({ ...moves(), system: SYSTEM_PROMPT })) expect(text, move).not.toMatch(/submit_/);
  });

  it("offers every tool the model is told to use: the evidence tool on a searching move, none on a move without searches", async () => {
    const t = setup((request) => (request.emit !== undefined ? { emit: { candidates: [], reasoning: "r" } } : { content: "done" }));
    const all = moves();
    await t.client.runTurn(t.request({ userContent: all.expand! }));
    await t.client.runTurn(t.request({ move: "conclude", submitTool: "submit_conclusion", maxSearches: 0, userContent: all.conclude! }));
    for (const [i, searches] of [[0, true], [2, false]] as const) {
      const sent = t.provider.requests[i]!;
      const text = sent.messages.map((m) => m.content).join("\n");
      expect(text).not.toMatch(/submit_/);
      expect(sent.tools.map((x) => x.id)).toEqual(searches ? ["splunk_query", SUBMIT_TOOL] : [SUBMIT_TOOL]);
    }
    expect(all.conclude).not.toContain("splunk_query");
  });
});
