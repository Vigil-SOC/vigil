import { describe, expect, it } from "vitest";
import { applyPatch, emptyPatch } from "../../../plugins/rootcause/v3/graph.js";
import { options, optionsText, resolveNext } from "../../../plugins/rootcause/v3/lead.js";
import type { TurnRequest } from "../../../plugins/rootcause/v3/model/client.js";
import { run } from "../../../plugins/rootcause/v3/orchestrator.js";
import { Registry } from "../../../plugins/rootcause/v3/tools/registry.js";
import { MemoryTrace } from "../../../plugins/rootcause/v3/trace.js";
import { apply, cfg, ev, node, seeded } from "./helpers.js";

describe("lead", () => {
  it("takes an available choice", () => {
    const g = seeded();
    expect(resolveNext({ move: "expand", target: "N1", why: "cause unknown" }, g, cfg, 1, []).decision).toMatchObject({ move: "expand", targetId: "N1", turn: 2 });
  });
  it("refuses a move or target that is not available", () => {
    const g = seeded();
    expect(resolveNext({ move: "pivot", target: "B9" }, g, cfg, 1, []).refused).toMatch(/not available/);
    expect(resolveNext({ move: "expand", target: "N42" }, g, cfg, 1, []).refused).toMatch(/available on: N1/);
  });
  it("resolves an event reported this turn by its label", () => {
    const g = seeded();
    const r = apply(g, { addNodes: [node("c", { label: "curl of setup.sh on node-a", evidence: [ev("direct")] })] });
    const created = [...new Set(Object.values(r.idMap))];
    expect(resolveNext({ move: "branch", target: "curl of setup.sh on node-a" }, g, cfg, 1, created).decision?.targetId).toBe(created[0]);
  });
  it("targets an event made on an earlier turn by id or exact label, and still refuses one the move does not offer", () => {
    const g = seeded();
    const r = apply(g, { addNodes: [node("c", { label: "curl of setup.sh on node-a", evidence: [ev("direct")] })] }, 2);
    const id = Object.values(r.idMap)[0]!;
    expect(resolveNext({ move: "branch", target: id }, g, cfg, 3, []).decision?.targetId).toBe(id);
    expect(resolveNext({ move: "branch", target: "Curl of setup.sh on node-a" }, g, cfg, 3, []).decision?.targetId).toBe(id);
    // N1 is the symptom: branch is not offered on it, even when it is named by label.
    expect(resolveNext({ move: "branch", target: g.nodes.N1!.label }, g, cfg, 3, []).refused).toMatch(/branch N1 \(named by label\) is not available: branch does not apply to it/);
  });
  it("of several events with one label, takes one the move applies to", () => {
    const g = seeded();
    const r = apply(g, { addNodes: [node("c", { label: g.nodes.N1!.label, domain: "proxy", evidence: [ev("direct")] })] }, 2);
    const id = Object.values(r.idMap)[0]!;
    // N1 comes first in the graph but is the symptom, where branch is not offered.
    expect(resolveNext({ move: "branch", target: g.nodes.N1!.label }, g, cfg, 3, []).decision?.targetId).toBe(id);
  });
  it("prefers an exact label over an earlier event whose label it is a prefix of", () => {
    const g = seeded();
    apply(g, { addNodes: [node("c", { label: "curl of setup.sh on node-a, then chmod", evidence: [ev("direct")] })] }, 1);
    const exact = Object.values(apply(g, { addNodes: [node("d", { label: "curl of setup.sh on node-a", evidence: [ev("direct")] })] }, 2).idMap)[0]!;
    expect(resolveNext({ move: "branch", target: "curl of setup.sh on node-a" }, g, cfg, 3, []).decision?.targetId).toBe(exact);
  });
  it("lists no branch from the symptom and no expand past the attempt cap", () => {
    const g = seeded();
    g.nodes.N1!.expandAttempts = cfg.maxExpandAttempts + 1;
    const o = options(g, cfg);
    expect(o.branch).toBeUndefined();
    expect(o.expand).toBeUndefined();
    expect(optionsText(g, cfg, { refused: "x" })).toContain("Your last choice was not used: x");
    expect(optionsText(g, cfg, {})).toContain("you will be asked to choose");
    expect(optionsText(g, cfg, {}, 0, true)).not.toContain("asked to choose");
    expect(optionsText(g, cfg, {}, 0, true)).toContain("or an event by its exact label.");
  });
  it("maps a pivot named by its event to that event's boundary, and a bare prediction id to the latest round", () => {
    const g = seeded();
    apply(g, { addNodes: [node("m", { kind: "boundary-marker", originNodeId: "N1", targetDomain: "email" })], addEdges: [{ tempId: "e", kind: "explains", source: "m", target: "N1", prior: 0.3, evidence: [] }] });
    expect(resolveNext({ move: "pivot", target: "N1" }, g, cfg, 1, []).decision?.targetId).toBe("B1");
    const p = (id: string) => ({ id, sourcetype: "edr", expect: "x", spl: "x", separates: [], next: false, status: "open" as const });
    g.theories = [{ id: "R2.H1", round: 2, story: "s", accountsFor: [], gaps: "", status: "live", support: 0, against: 0, predictions: [p("R2.P3"), p("R2.P4")] }];
    expect(resolveNext({ move: "test", target: "P3" }, g, cfg, 1, []).decision?.targetId).toBe("R2.P3");
    expect(resolveNext({ move: "test", target: "H1.P4" }, g, cfg, 1, []).decision?.targetId).toBe("R2.P4");
  });
  it("carries the model's chosen source on the decision", () => {
    const g = seeded();
    apply(g, { addNodes: [node("m", { kind: "boundary-marker", originNodeId: "N1", targetDomain: "edr" })], addEdges: [{ tempId: "e", kind: "explains", source: "m", target: "N1", prior: 0.3, evidence: [] }] });
    expect(resolveNext({ move: "pivot", target: "B1", source: "github" }, g, cfg, 1, []).decision).toMatchObject({ move: "pivot", targetId: "B1", source: "github" });
  });
  it("brings up untested stories after a stretch without a test", () => {
    const g = seeded();
    const p = (id: string) => ({ id, sourcetype: "github", expect: "a PR by x", spl: "x", separates: [], next: false, status: "open" as const });
    g.theories = [{ id: "R1.H1", round: 1, story: "s", accountsFor: [], gaps: "", status: "live", support: 0, against: 0, predictions: [p("R1.P1")] }];
    g.theorizeCount = 1; g.theorizedTurn = 3;
    expect(optionsText(g, cfg, {}, 5)).not.toContain("untested predictions");
    expect(optionsText(g, cfg, {}, 10)).toContain("untested predictions (7 turns since a test or a new round): R1.P1");
  });
  it("probe turns an unread rare record into a whole-range prediction and tests it next", () => {
    const g = seeded();
    g.rare = "- github:\n  actor (3 distinct) [r29]: zed ×3 (2026-03-11 14:53 → 2026-03-11 14:54, h); bot ×123 (2026-03-11 → 2026-03-13, h)";
    expect(optionsText(g, cfg, {}, 8)).toContain('"zed" (actor, github, ×3)');
    const r = resolveNext({ move: "probe", target: "zed", why: "rare" }, g, cfg, 8, [], { index: "lab", range: ["2026-03-11T00:00:00Z", "2026-03-14T00:00:00Z"] });
    expect(r.decision).toMatchObject({ move: "test", targetId: "PROBE1" });
    expect(g.theories?.some((t) => t.id === "PROBES")).toBeFalsy();
    applyPatch(g, { ...emptyPatch(), addProbe: r.probe! }, 8, cfg);
    const pr = g.theories!.find((t) => t.id === "PROBES")!.predictions[0]!;
    expect(pr).toMatchObject({ sourcetype: "github", earliest: "2026-03-11T00:00:00Z", status: "open" });
    expect(pr.spl).toContain('"zed"');
    expect(resolveNext({ move: "probe", target: "nope" }, g, cfg, 8, [], { index: "lab", range: ["a", "b"] }).refused).toMatch(/not available/);
  });
});

// The loop with a stub model: who names each move, and what the next prompt says.
describe("the model names every move", () => {
  const alert = { id: "a", source: "edr", title: "alert", observedAt: "2026-01-01T00:00:00Z", artifacts: [], raw: {} };
  const conclusion = { conclusion: "done", chain: [], openQuestions: [], reasoning: "" };
  const drive = async (answer: (req: TurnRequest, n: number, budget: { usd: number }) => unknown, graph?: ReturnType<typeof seeded>, failing: (n: number) => string | undefined = () => undefined) => {
    const reqs: TurnRequest[] = [];
    const budget = { usd: 0 };
    const model = { runTurn: async (req: TurnRequest) => { reqs.push(req); const failed = failing(reqs.length); return failed !== undefined ? { submission: null, toolCalls: [], failed } : { submission: answer(req, reqs.length, budget) ?? null, toolCalls: [] }; } };
    const trace = new MemoryTrace("t");
    const r = await run(alert, { ...cfg, rareSweep: false, maxUsd: 100 }, { model, tools: new Registry(), trace, budget, domains: ["edr"], clock: () => new Date() }, { runId: "t", range: ["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"], index: "x", graph });
    return { reqs, r, trace };
  };

  it("asks for the first move, tells a refusal and a silent turn to the next choose turn, and runs each choice", async () => {
    const { reqs, trace } = await drive((req, n, budget) => {
      if (req.move === "conclude") return conclusion;
      if (req.move === "choose") { if (n === 5) budget.usd = 1e6; return { move: "strengthen", target: "N1", why: "the alert is unconfirmed" }; }
      // The first strengthen names a move that is not open; the second gives nothing valid.
      return n === 2 ? { evidence: [], reasoning: "r", next: { move: "pivot", target: "B9", why: "x" } } : null;
    });
    expect(reqs.map((r) => r.move)).toEqual(["choose", "strengthen", "choose", "strengthen", "choose", "conclude"]);
    expect(reqs[0]!.maxSearches).toBe(0);
    expect(reqs[0]!.userContent).not.toContain("Your last choice was not used");
    expect(reqs[2]!.userContent).toContain("Your last choice was not used: pivot is not available now.");
    expect(reqs[4]!.userContent).toContain("Your last turn, on strengthen N1, gave no valid submission");
    // A choose turn is a real turn: a decision entry, with no priority.
    const decisions = trace.chronological().filter((e) => e.type === "decision").map((e) => e.payload);
    expect(decisions[0]).toMatchObject({ move: "choose", targetId: "N1" });
    expect(decisions.every((d) => !("priority" in (d as object)))).toBe(true);
  });

  it("tells the model a provider error lost its choose turn, not that the choice was invalid, and asks again without a retry count", async () => {
    const { reqs, trace } = await drive((req, n, budget) => {
      if (req.move === "conclude") return conclusion;
      if (n === 3) budget.usd = 1e6;
      return req.move === "choose" ? { move: "strengthen", target: "N1", why: "the alert is unconfirmed" } : null;
    }, undefined, (n) => (n === 1 ? "502 failed to execute HTTP request to provider API" : undefined));
    expect(reqs.map((r) => r.move)).toEqual(["choose", "choose", "strengthen", "conclude"]);
    expect(reqs[1]!.userContent).not.toContain("not a valid choice");
    expect(reqs[1]!.userContent).not.toContain("Your last choice was not used");
    expect(reqs[1]!.userContent).toContain("lost to a provider error (502 failed to execute HTTP request to provider API)");
    expect(reqs[2]!.userContent).not.toContain("provider error");
    const decisions = trace.chronological().filter((e) => e.type === "decision").map((e) => e.payload as { move: string; reason: string });
    expect(decisions[1]).toMatchObject({ move: "choose", reason: "502 failed to execute HTTP request to provider API" });
  });

  it("treats a provider error with no message as a lost choose turn too", async () => {
    const { reqs } = await drive((req, n, budget) => {
      if (req.move === "conclude") return conclusion;
      if (n === 3) budget.usd = 1e6;
      return req.move === "choose" ? { move: "strengthen", target: "N1", why: "the alert is unconfirmed" } : null;
    }, undefined, (n) => (n === 1 ? "" : undefined));
    expect(reqs.map((r) => r.move)).toEqual(["choose", "choose", "strengthen", "conclude"]);
    expect(reqs[1]!.userContent).not.toContain("not a valid choice");
    expect(reqs[1]!.userContent).toContain("Your last turn was lost to a provider error before your choice was judged");
  });

  it("runs the move a submission names without a choose turn", async () => {
    const { reqs } = await drive((req, n, budget) => {
      if (req.move === "conclude") return conclusion;
      if (req.move === "choose") return { move: "strengthen", target: "N1", why: "first look" };
      if (n === 2) return { evidence: [], reasoning: "r", next: { move: "expand", target: "N1", why: "its cause is unknown" } };
      budget.usd = 1e6;
      return { candidates: [], reasoning: "r" };
    });
    expect(reqs.map((r) => r.move)).toEqual(["choose", "strengthen", "expand", "conclude"]);
  });

  it("keeps back one more move at the average cost plus the closing turn, and concludes rather than start a move it cannot pay for", async () => {
    // Each turn costs $30 of $100: after two the average is 30, the reserve 60 and 40 is left.
    const { reqs, r } = await drive((req, _n, budget) => {
      if (req.move === "conclude") return conclusion;
      budget.usd += 30;
      return req.move === "choose" ? { move: "strengthen", target: "N1", why: "the alert is unconfirmed" } : null;
    });
    expect(reqs.map((x) => x.move)).toEqual(["choose", "strengthen", "conclude"]);
    expect(r.exit).toBe("budget-exhausted");
    expect(r.report).toContain("exit **budget-exhausted**");
    expect(r.report).toContain("done");
  });

  it.each([[true, "root-cause-found"], [false, "inconclusive"]])("ends when no move is open (root %s: %s), after the closing conclude turn", async (isRoot, exit) => {
    const g = seeded();
    apply(g, { incrementExpand: ["N1", "N1", "N1"], ...(isRoot ? { setFlags: [{ nodeId: "N1", isRoot: true }] } : {}) });
    for (let t = 2; t <= 4; t++) apply(g, { incrementAttempts: ["N1"] }, t);
    expect(options(g, cfg)).toEqual({});
    const { reqs, r } = await drive((req) => (req.move === "conclude" ? conclusion : null), g);
    expect(reqs.map((x) => x.move)).toEqual(["conclude"]);
    expect(r.exit).toBe(exit);
  });
});
