import { describe, expect, it } from "vitest";
import { options, optionsText, resolveNext } from "../../../plugins/rootcause/v3/lead.js";
import { apply, cfg, ev, node, seeded } from "./helpers.js";

describe("lead", () => {
  it("takes an available choice", () => {
    const g = seeded();
    expect(resolveNext({ move: "expand", target: "N1", why: "cause unknown" }, g, cfg, 1, []).decision).toMatchObject({ move: "expand", targetId: "N1", turn: 2, priority: -1 });
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
  it("lists no branch from the symptom and no expand past the attempt cap", () => {
    const g = seeded();
    g.nodes.N1.expandAttempts = cfg.maxExpandAttempts + 1;
    const o = options(g, cfg);
    expect(o.branch).toBeUndefined();
    expect(o.expand).toBeUndefined();
    expect(optionsText(g, cfg, { refused: "x" })).toContain("Your last choice was not used: x");
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
    g.rare = "- github:\n  actor (3 distinct) [tc_29]: zed ×3 (2026-03-11 14:53 → 2026-03-11 14:54, h); bot ×123 (2026-03-11 → 2026-03-13, h)";
    expect(optionsText(g, cfg, {}, 8)).toContain('"zed" (actor, github, ×3)');
    const r = resolveNext({ move: "probe", target: "zed", why: "rare" }, g, cfg, 8, [], { index: "lab", range: ["2026-03-11T00:00:00Z", "2026-03-14T00:00:00Z"] });
    expect(r.decision).toMatchObject({ move: "test", targetId: "PROBE1" });
    const pr = g.theories!.find((t) => t.id === "PROBES")!.predictions[0];
    expect(pr).toMatchObject({ sourcetype: "github", earliest: "2026-03-11T00:00:00Z", status: "open" });
    expect(pr.spl).toContain('"zed"');
    expect(resolveNext({ move: "probe", target: "nope" }, g, cfg, 8, [], { index: "lab", range: ["a", "b"] }).refused).toMatch(/not available/);
  });
});
