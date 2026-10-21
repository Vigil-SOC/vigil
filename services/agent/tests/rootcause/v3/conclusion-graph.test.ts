// The model's conclusion is checked against the graph: a rejected one is shown unconfirmed in the report, the terminal
// summary and the projection, and the report's open questions are the projection's.
import { describe, expect, it } from "vitest";
import type { AgentEvent } from "../../../contracts/events.js";
import { emptyPatch, type GraphPatch } from "../../../plugins/rootcause/v3/graph.js";
import { conclusionOf } from "../../../plugins/rootcause/v3/engine.js";
import { fold, type V3Kinds } from "../../../plugins/rootcause/v3/ledger.js";
import { rootCauseV3Projection } from "../../../plugins/rootcause/v3/projection.js";
import { renderReport } from "../../../plugins/rootcause/v3/report.js";
import { MemoryTrace, type TraceEntry } from "../../../plugins/rootcause/v3/trace.js";
import type { Alert } from "../../../plugins/rootcause/v3/types.js";
import { cfg, ev, node } from "./helpers.js";

const alert: Alert = { id: "a1", source: "edr", title: "Backup catalog deleted", observedAt: "2026-05-05T10:00:00Z", artifacts: [], raw: {} };
const solid = (call: string) => [ev("direct", "supports", [call]), ev("corroborating", "supports", [`${call}b`])];
const refuted = (call: string) => [ev("direct", "refutes", [call], "no such link"), ev("corroborating", "refutes", [`${call}b`], "the logs disagree")];
const edge = (tempId: string, source: string, target: string, evidence: ReturnType<typeof solid>) => ({ tempId, kind: "explains" as const, source, target, prior: 0.3, evidence });
const conclude: GraphPatch = { ...emptyPatch(), setConclusion: { text: "A mail dropped the implant.", rootCause: "N3", chain: ["N3", "N2", "N1"], openQuestions: ["who sent the mail"], turn: 2 } };

// N3 -> N2 -> N1 (the alert). The link N2 -> N1 is E1; it is refuted when `rejectE1`. `concluded` patches the conclusion.
function journal(rejectE1: boolean, concluded: Partial<NonNullable<GraphPatch["setConclusion"]>> = {}) {
  const patch: GraphPatch = {
    ...emptyPatch(),
    addNodes: [node("implant", { evidence: solid("r1") }), node("mail", { isRoot: !rejectE1, evidence: solid("r2") })],
    addEdges: [edge("e1", "implant", "N1", rejectE1 ? refuted("x1") : solid("x1")), edge("e2", "mail", "implant", solid("x2"))],
  };
  const events: AgentEvent<V3Kinds>[] = [];
  let id = 0;
  const push = (kind: string, payload: unknown) =>
    events.push({ run_id: "r1", run_kind: "root_cause", seq: events.length, ts: new Date().toISOString(), kind, payload: JSON.parse(JSON.stringify(payload)), schema_version: 1 } as AgentEvent<V3Kinds>);
  const entry = (turn: number, p: GraphPatch): TraceEntry => ({ id: `T${String(++id).padStart(6, "0")}`, ts: `2026-05-05T12:00:0${id}Z`, runId: "r1", turn, correlationId: "N1", relatedIds: [], type: "graph-patch", payload: { patch: p } });
  push("run", { run_kind: "root_cause", spec: { sections: { engine: "rootcause-v3" } } });
  push("rootcause_setup", { cfg, alert, index: "lab", range: ["2026-05-05T00:00:00Z", "2026-05-05T12:00:00Z"], survey: { text: "", domains: [], sizes: {} }, environment: null });
  push("rootcause_turn", { turn: 1, entries: [entry(1, patch)] });
  push("rootcause_turn", { turn: 2, entries: [entry(2, { ...conclude, setConclusion: { ...conclude.setConclusion!, ...concluded } })] });
  push("rootcause_exit", { exit: "inconclusive", report: "r", entries: [] });
  return events;
}
const render = (events: AgentEvent<V3Kinds>[]) => { const f = fold(events); return renderReport(f.graph!, new MemoryTrace("r1", () => new Date(), f.entries), f.exit, alert); };

describe("a conclusion the graph rejects", () => {
  const events = journal(true);
  const md = render(events);
  const view = rootCauseV3Projection("r1", events as never);

  it("is shown unconfirmed in the report, naming what the graph rejected, under the graph's verdict", () => {
    expect(md).toContain("**Disposition:** Inconclusive");
    expect(md).toContain("## Investigator's conclusion (unconfirmed)");
    expect(md).toMatch(/Unconfirmed: the graph rejects part of this conclusion \(.*E1 \(N2 → N1\) ruled out at turn \d+/);
    expect(md).toContain("Root cause the investigator named, unconfirmed: **N3**");
    expect(md).not.toContain("Root cause named:");
  });

  it("reaches the terminal summary", () => {
    expect(conclusionOf(md)).toMatch(/^Unconfirmed: the graph rejects part of this conclusion/);
  });

  it("is not the projection's root cause, and says which parts were rejected", () => {
    expect(view.account!.root_cause).toBeNull(); // the graph marks no root: the model's N3 is not taken for one
    expect(view.account!.conclusion).toMatchObject({ unconfirmed: true, rejected: expect.arrayContaining([{ id: "E1", reason: expect.stringContaining("E1") }]) });
    expect(view.account!.conclusion!.text).toMatch(/^Unconfirmed:/);
    expect(view.account!.disposition).toMatch(/^Inconclusive/);
  });

  it("lists the ruled-out edge and the model's open question, one list in the report and the projection", () => {
    expect(view.open_questions.map((q) => [q.kind, q.id])).toEqual([["ruled-out", "E1"], ["model", "Q1"]]);
    const section = md.split("## 6. Open questions\n\n")[1]!.trim().split("\n").map((l) => l.replace(/^- /, ""));
    expect(section).toEqual(view.open_questions.map((q) => q.text));
    expect(md).not.toContain("Still open:");
  });
});

describe("a conclusion the graph supports", () => {
  const events = journal(false);
  const md = render(events);
  const view = rootCauseV3Projection("r1", events as never);

  it("renders as before", () => {
    expect(md).toContain("## Investigator's conclusion\n\nA mail dropped the implant.\n\nRoot cause named: **N3**");
    expect(md).not.toContain("nconfirmed");
    expect(view.account!.root_cause).toMatchObject({ id: "N3" });
    expect(view.account!.conclusion).toMatchObject({ text: "A mail dropped the implant.", unconfirmed: false, rejected: [] });
    expect(view.open_questions).toEqual([{ kind: "model", id: "Q1", text: "who sent the mail" }]);
  });
});

describe("a conclusion naming the alert as its root", () => {
  // E1 refuted, so the graph marks no root; the chain is the alert alone, which needs no settling.
  const events = journal(true, { rootCause: "N1", chain: ["N1"] });
  const view = rootCauseV3Projection("r1", events as never);

  it("is unconfirmed: the alert is not a cause", () => {
    expect(render(events)).toContain("## Investigator's conclusion (unconfirmed)");
    expect(view.account!.conclusion).toMatchObject({ unconfirmed: true, rejected: [{ id: "N1", reason: "N1 is the alert, not a cause" }] });
    expect(view.account!.root_cause).toBeNull();
  });
});

describe("a blank open question from the model", () => {
  const events = journal(false, { openQuestions: ["  who sent\n the mail ", " \n "] });

  it("is dropped, so the report prints no empty bullet", () => {
    expect(rootCauseV3Projection("r1", events as never).open_questions).toEqual([{ kind: "model", id: "Q1", text: "who sent the mail" }]);
    expect(render(events)).not.toMatch(/^- *$/m);
  });
});
