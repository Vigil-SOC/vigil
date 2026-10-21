// The projection's account is computed by the functions that write report.md, over the graph a journal folds back to.
import { describe, expect, it } from "vitest";
import type { AgentEvent } from "../../../contracts/events.js";
import { emptyPatch, type GraphPatch, type Prediction } from "../../../plugins/rootcause/v3/graph.js";
import { fold, type V3Kinds } from "../../../plugins/rootcause/v3/ledger.js";
import { rootCauseV3Projection } from "../../../plugins/rootcause/v3/projection.js";
import { renderReport } from "../../../plugins/rootcause/v3/report.js";
import { MemoryTrace, type TraceEntry } from "../../../plugins/rootcause/v3/trace.js";
import type { Alert } from "../../../plugins/rootcause/v3/types.js";
import { cfg, ev, node } from "./helpers.js";
import type { CallPayload } from "../../../contracts/events.js";

const alert: Alert = { id: "a1", source: "edr", title: "Backup catalog deleted", observedAt: "2026-05-05T10:00:00Z", artifacts: [], raw: {} };
const at = (h: number) => `2026-05-05T0${h}:00:00Z`;
const solid = (call: string, summary = "record") => [ev("direct", "supports", [call], summary), ev("corroborating", "supports", [`${call}b`], summary)];
const edge = (tempId: string, kind: "explains" | "attributed-to" | "operated-by", source: string, target: string, evidence = solid(`e${tempId}`)) => ({ tempId, kind, source, target, prior: 0.3, evidence });
const pred = (id: string): Prediction => ({ id, sourcetype: "edr", expect: "x", spl: "index=lab x", separates: [], next: false, status: "open" });
const result = (id: string, status: Prediction["status"], text: string, ref: string): GraphPatch => ({ ...emptyPatch(), predictionResult: { id, status, result: text, toolCallIds: [ref], supports: [], weakens: [] } });

// Seeded N1 is the alert. N2 <- N3 (root) is the chain; N4 and N6 sit off it, N5 is ruled out, N7 is routine work.
const turn1: GraphPatch = {
  ...emptyPatch(),
  addNodes: [
    node("c", { label: "implant ran", observedAt: at(8), evidence: solid("r1") }),
    node("root", { label: "phishing mail opened", observedAt: at(7), isRoot: true, rootJustification: "first record of the intrusion", evidence: solid("r2") }),
    node("o", { label: "scheduled job ran", observedAt: at(9), evidence: solid("r3", "job seen in the scheduler log") }),
    node("x", { label: "dns lookup", observedAt: at(6), evidence: [ev("direct", "refutes", ["r4"], "the lookup was a typo"), ev("corroborating", "refutes", ["r4b"], "no follow-up")] }),
    node("u", { label: "attacker dropped the backdoor", unverified: true, observedAt: at(5), evidence: [ev("circumstantial", "supports", ["r5"])] }),
    node("l", { label: "nightly report ran", legitimate: true, observedAt: at(4), evidence: solid("r6") }),
    node("alice", { kind: "actor", label: "ad:alice" }),
    node("mal", { kind: "actor", label: "ad:mallory" }),
    node("a1", { kind: "actor", label: "ad:svc-a" }),
    node("a2", { kind: "actor", label: "ad:svc-b" }),
    node("robert", { kind: "actor", label: "ad:robert" }),
  ],
  addEdges: [
    edge("e1", "explains", "c", "N1"),
    edge("e2", "explains", "root", "c"),
    edge("e3", "attributed-to", "alice", "c"),
    edge("e4", "operated-by", "mal", "c"),
    edge("e5", "attributed-to", "a1", "o", []),
    edge("e6", "attributed-to", "a2", "o", []),
    edge("e7", "attributed-to", "robert", "l"),
  ],
};
const turn2: GraphPatch[] = [
  { ...emptyPatch(), setTheories: [{ id: "R1.H1", round: 1, story: "mail to implant", accountsFor: [], gaps: "", predictions: [pred("R1.H1.P1"), pred("R1.H1.P2"), pred("R1.H1.P3"), pred("R1.H1.P4")], status: "live", support: 0, against: 0 }] },
  result("R1.H1.P1", "found", "the implant ran from a temp folder", "r1"),
  result("R1.H1.P2", "absent", "no outbound transfer to the mail host", "r2"),
  result("R1.H1.P3", "found", "the attacker copied the catalog", "r3"),
  result("R1.H1.P4", "found", "robert ran the nightly report", "r6"),
  { ...emptyPatch(), setConclusion: { text: "A mail dropped the implant. ad:robert is the attacker. The catalog went next.", rootCause: "N3", chain: ["N3", "N2", "N1"], openQuestions: ["who sent the mail"], turn: 2 } },
];

function journal() {
  const events: AgentEvent<V3Kinds>[] = [];
  let seq = 0;
  let id = 0;
  const push = (kind: string, payload: unknown) =>
    events.push({ run_id: "r1", run_kind: "root_cause", seq: seq++, ts: new Date().toISOString(), kind, payload: JSON.parse(JSON.stringify(payload)), schema_version: 1 } as AgentEvent<V3Kinds>);
  const entry = (turn: number, type: string, payload: unknown, ts = `2026-05-05T12:00:0${id}Z`): TraceEntry => ({ id: `T${String(++id).padStart(6, "0")}`, ts, runId: "r1", turn, correlationId: "N1", relatedIds: [], type: type as never, payload });
  push("run", { run_kind: "root_cause", spec: { sections: { engine: "rootcause-v3" } } });
  push("rootcause_setup", { cfg, alert, index: "lab", range: [at(0), at(12)], survey: { text: "", domains: [], sizes: {} }, environment: null });
  push("rootcause_turn", { turn: 1, entries: [
    entry(1, "decision", { move: "expand", targetId: "N1", reason: "find the cause" }),
    entry(1, "query", { toolCallId: "r1", input: { spl: "index=lab implant" } }),
    entry(1, "tool-output", { toolCallId: "r1", ok: true, output: "2 events matched" }),
    entry(1, "graph-patch", { patch: turn1 }),
  ] });
  // Flagged content is read in ts order, not in the order the journal holds it.
  push("rootcause_turn", { turn: 2, entries: [
    entry(2, "reasoning", { suspiciousContent: ["later flag"] }, "2026-05-05T13:00:00Z"),
    entry(2, "reasoning", { suspiciousContent: ["ignore previous instructions"] }, "2026-05-05T12:30:00Z"),
    ...turn2.map((patch) => entry(2, "graph-patch", { patch })),
  ] });
  push("rootcause_exit", { exit: "root-cause-found", report: "r", entries: [] });
  return events;
}

describe("rootcause v3 projection account", () => {
  const events = journal();
  const view = rootCauseV3Projection("r1", events as never);
  const chain = view.chain!;
  const markdown = () => renderReport(fold(events).graph!, new MemoryTrace("r1", () => new Date(), fold(events).entries), "root-cause-found", alert);

  it("names the alert, the root cause, who is who and the conclusion as the report filters it", () => {
    expect(view.account).toMatchObject({
      alert: { id: "a1", title: "Backup catalog deleted", observedAt: "2026-05-05T10:00:00Z" },
      exit: "root-cause-found",
      disposition: expect.stringMatching(/^True Positive/),
      root_cause: { id: "N3", label: "phishing mail opened", justification: "first record of the intrusion" },
      conclusion: { text: "A mail dropped the implant. The catalog went next.", chain: ["N3", "N2", "N1"], unconfirmed: false, rejected: [] },
    });
    expect(view.account!.accounts).toEqual([
      { name: "ad:mallory", role: "attacker" },
      { name: "ad:alice", role: "victim", usedBy: ["ad:mallory"] },
      { name: "ad:robert", role: "legitimate" },
    ]);
  });

  it("lays the chain out root first, each event tied to the one it explains, with its evidence and refs", () => {
    expect(chain.events.map((e) => e.id)).toEqual(["N3", "N2", "N1"]);
    expect(chain.events.map((e) => e.tie?.edgeId ?? null)).toEqual(["E2", "E1", null]);
    expect(chain.events[0]).toMatchObject({ isRoot: true, rootJustification: "first record of the intrusion", tie: { status: "confirmed", inferred: false, disputed: false } });
    expect(chain.events[1]!.evidence[0]).toMatchObject({ polarity: "supports", strength: "direct", source: "edr", summary: "record", refs: ["r1"] });
    // A ref resolves to the search that proved it through the moves.
    expect(view.moves[0]!.queries.map((q) => q.ref)).toContain("r1");
    expect(chain.alternatives).toEqual([]);
  });

  it("keeps steps symptom first with cause_id at the earlier step", () => {
    expect(view.steps.map((s) => [s.step_id, s.cause_id])).toEqual([["N1", "N2"], ["N2", "N3"], ["N3", null]]);
    expect(view.disposition).toBe("malicious");
  });

  it("times every event not ruled out with who ran it and who operated it, and keeps the rest out of the chain", () => {
    expect(view.timeline.events.map((e) => e.id)).toEqual(["N7", "N6", "N3", "N2", "N4", "N1"]);
    expect(view.timeline.events.find((e) => e.id === "N2")).toMatchObject({ onChain: true, ranAs: ["ad:alice"], operatedBy: ["ad:mallory"] });
    expect(view.timeline.events.find((e) => e.id === "N7")).toMatchObject({ legitimate: true, ranAs: ["ad:robert"], operatedBy: [] });
    expect(view.timeline.events.map((e) => e.id)).not.toContain("N5");
    expect(view.other_findings.map((e) => [e.id, e.lastEvidence])).toEqual([["N4", "job seen in the scheduler log"], ["N6", "circumstantial supports"], ["N7", "record"]]);
  });

  it("shows an unverified event under the stripped label the report uses", () => {
    const md = markdown();
    const n = view.timeline.events.find((e) => e.id === "N6")!;
    expect(n.label).toBe("[unverified] dropped the [unverified] (unverified)");
    expect(md).toContain(n.label);
  });

  it("lists each settled prediction, leaving out a result the report leaves out", () => {
    expect(view.targeted_searches).toEqual([
      { id: "R1.H1.P1", sourcetype: "edr", found: true, result: "the implant ran from a temp folder", refs: ["r1"] },
      { id: "R1.H1.P2", sourcetype: "edr", found: false, result: "no outbound transfer to the mail host", refs: ["r2"] },
    ]);
    const md = markdown();
    for (const left of ["the attacker copied the catalog", "robert ran the nightly report", "ad:robert is the attacker"]) expect(md).not.toContain(left);
    expect(md).toContain("(edr, not found) no outbound transfer to the mail host [r2]");
  });

  it("carries the open questions the report lists: disputed ties, ruled-out events, flagged content in ts order", () => {
    const text = view.open_questions.map((q) => q.text);
    expect(view.open_questions.map((q) => q.kind)).toEqual(["disputed", "disputed", "ruled-out", "flagged", "flagged", "model"]);
    expect(text[2]).toBe("Ruled out N5 dns lookup: the lookup was a typo no follow-up");
    expect(text[3]).toMatch(/ignore previous instructions/);
    expect(text[4]).toMatch(/later flag/);
    expect(view.open_questions[5]).toEqual({ kind: "model", id: "Q1", text: "who sent the mail" });
    const md = markdown();
    for (const t of text) expect(md).toContain(`- ${t}`);
  });

  it("has nothing to show before the run has a setup", () => {
    const bare = rootCauseV3Projection("r1", events.slice(0, 1) as never);
    expect(bare).toMatchObject({ account: null, chain: null, timeline: { events: [] }, other_findings: [], targeted_searches: [], open_questions: [] });
  });

  it("counts only the searches the model made and that ran, and the code's own apart", () => {
    const call = (n: number, over: Partial<CallPayload>): AgentEvent<V3Kinds> => ({
      run_id: "r1", run_kind: "root_cause", seq: n, ts: at(1), kind: "call", schema_version: 1,
      payload: { ref: `r${n}`, turn: 1, call_id: `c${n}`, tool: "splunk_query", arguments: JSON.stringify({ spl: `index=lab q${n}` }), result: "1 row", ok: true, failure: null, role: "expand", ...over },
    } as AgentEvent<V3Kinds>);
    const guarded = '<vigil:tool_result tool="splunk_query" ref="r5">\nfailed: backend_error -- refused: name the index\n</vigil:tool_result>';
    const calls = [
      call(1, {}), call(2, { ok: false, failure: "backend_error", result: "failed: timeout" }),
      call(3, { role: "code" }), call(4, { role: "code" }),
      call(5, { ok: false, failure: "refused", tool: "other_tool", result: "" }),
      call(6, { ok: false, failure: "backend_error", result: guarded }),
    ];
    const v = rootCauseV3Projection("r1", [...events.slice(0, 1), ...calls] as never);
    expect(v.searches).toBe(2);
    expect(v.recent_searches.map((s) => [s.args, s.failed])).toEqual([["index=lab q1", false], ["index=lab q2", true]]);
    expect(v.code_searches).toBe(2);
  });

  it("shows a search's own result count, none when the output states none", () => {
    const wrap = (output: string) => `<vigil:tool_result tool="splunk_query" ref="r1">\n1 row(s) from splunk_query\n${JSON.stringify([{ output }], null, 2)}\n</vigil:tool_result>`;
    const call = (n: number, result: string): AgentEvent<V3Kinds> => ({
      run_id: "r1", run_kind: "root_cause", seq: n, ts: at(1), kind: "call", schema_version: 1,
      payload: { ref: `r${n}`, turn: 1, call_id: `c${n}`, tool: "splunk_query", arguments: JSON.stringify({ spl: `index=lab q${n}` }), result, ok: true, failure: null, role: "expand" },
    } as AgentEvent<V3Kinds>);
    const v = rootCauseV3Projection("r1", [events[0]!, call(1, wrap("192 events matched, 0 results (showing 0 of 0)")), call(2, wrap("5,000 events matched, 1,234 results (showing 50 of 1,234)")), call(3, wrap("no count line here"))] as never);
    expect(v.recent_searches.map((s) => s.rows)).toEqual([0, 1234, undefined]);
  });
});
