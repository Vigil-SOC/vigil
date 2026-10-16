import { describe, expect, it } from "vitest";
import { createGraph } from "../../../plugins/rootcause/v3/graph.js";
import { isStalled, selectFrontier } from "../../../plugins/rootcause/v3/frontier.js";
import { apply, cfg, ev, node, seeded } from "./helpers.js";

const link = (tempId: string, source: string, target: string, evidence = [ev("direct", "supports", [`tc_${tempId}`])]) =>
  ({ tempId, kind: "explains" as const, source, target, prior: 0.3, evidence });
const solid = (tempId: string, over = {}) => node(tempId, { evidence: [ev("direct", "supports", [`tc_${tempId}`]), ev("corroborating", "supports", [`tc_${tempId}b`])], ...over });

describe("frontier", () => {
  it("P0: strengthen an unconfirmed symptom", () => {
    const g = createGraph("t");
    apply(g, { addNodes: [node("S", { isSymptom: true })] });
    expect(selectFrontier(g, cfg, 2)).toMatchObject({ move: "strengthen", targetId: "N1", priority: 0 });
  });
  it("P6: expand a confirmed top with no cause", () => {
    expect(selectFrontier(seeded(), cfg, 2)).toMatchObject({ move: "expand", targetId: "N1", priority: 6 });
  });
  it("P1: pivot on an open marker", () => {
    const g = seeded();
    apply(g, { addNodes: [node("m", { kind: "boundary-marker", originNodeId: "N1", targetDomain: "email" })], addEdges: [{ ...link("e", "m", "N1"), evidence: [] }] });
    expect(selectFrontier(g, cfg, 2)).toMatchObject({ move: "pivot", targetId: "B1", priority: 1 });
  });
  it("P2: disputed attribution first", () => {
    const g = seeded();
    apply(g, { addNodes: [node("a1", { kind: "actor", label: "x" }), node("a2", { kind: "actor", label: "y" })],
      addEdges: [{ tempId: "x", kind: "attributed-to", source: "a1", target: "N1", prior: 0.3, evidence: [] }, { tempId: "y", kind: "attributed-to", source: "a2", target: "N1", prior: 0.3, evidence: [] }] });
    expect(selectFrontier(g, cfg, 2)).toMatchObject({ move: "strengthen", priority: 2, targetId: "E1" });
  });
  it("undisputed unconfirmed attribution waits for P4, behind the causal chain", () => {
    const g = seeded();
    apply(g, { addNodes: [node("a1", { kind: "actor", label: "x" }), solid("c")],
      addEdges: [{ tempId: "x", kind: "attributed-to", source: "a1", target: "N1", prior: 0.3, evidence: [] }, link("e", "c", "N1", [])] });
    expect(selectFrontier(g, cfg, 2)).toMatchObject({ move: "strengthen", targetId: "E2", priority: 3 });
    apply(g, { addEvidence: [{ targetId: "E2", evidence: ev("direct", "supports", ["q"]) }, { targetId: "E2", evidence: ev("corroborating", "supports", ["r"]) }] });
    expect(selectFrontier(g, cfg, 3)).toMatchObject({ move: "strengthen", targetId: "E1", priority: 4 });
  });
  it("P3: weakest suspected link", () => {
    const g = seeded();
    apply(g, { addNodes: [solid("c")], addEdges: [link("e", "c", "N1", [])] });
    expect(selectFrontier(g, cfg, 2)).toMatchObject({ move: "strengthen", targetId: "E1", priority: 3 });
  });
  it("P4: suspected node on the path", () => {
    const g = seeded();
    apply(g, { addNodes: [node("c")], addEdges: [link("e", "c", "N1", [ev("direct", "supports", ["a"]), ev("corroborating", "supports", ["b"])])] });
    expect(selectFrontier(g, cfg, 2)).toMatchObject({ move: "strengthen", targetId: "N2", priority: 4 });
  });
  it("P5: fork when a link stalls", () => {
    const g = seeded();
    apply(g, { addNodes: [solid("c")], addEdges: [link("e", "c", "N1", [])] });
    for (let t = 2; t <= 4; t++) apply(g, { incrementAttempts: ["E1"] }, t);
    expect(isStalled(g.edges.E1, cfg)).toBe(true);
    expect(selectFrontier(g, cfg, 5)).toMatchObject({ move: "fork", targetId: "N1", priority: 5 });
  });
  it("stall by small movement over the last two attempts", () => {
    const g = seeded();
    apply(g, { addNodes: [node("c")] });
    apply(g, { incrementAttempts: ["N2"] }, 2);
    apply(g, { incrementAttempts: ["N2"] }, 3);
    expect(isStalled(g.nodes.N2, cfg)).toBe(true);
  });
  it("P7: alternative path when the best is done", () => {
    const g = seeded();
    apply(g, { addNodes: [solid("root", { isRoot: true }), solid("alt")], addEdges: [
      link("e1", "root", "N1", [ev("direct", "supports", ["a"]), ev("corroborating", "supports", ["b"])]), link("e2", "alt", "N1", [ev("corroborating", "supports", ["c"])])] });
    const d = selectFrontier(g, cfg, 2)!;
    expect(d.priority).toBe(7);
    expect(d.targetId).toBe("E2");
  });
  it("null when nothing qualifies", () => {
    const g = seeded();
    apply(g, { setFlags: [{ nodeId: "N1", isRoot: true }] });
    expect(selectFrontier(g, cfg, 2)).toBeNull();
  });
  it("tie-break: lower confidence first", () => {
    const g = seeded();
    apply(g, { addNodes: [solid("a"), node("b", { evidence: [ev("corroborating", "supports", ["q"]), ev("direct", "supports", ["r"])] }), solid("c")],
      addEdges: [link("e1", "a", "N1", []), link("e2", "b", "N2", [ev("circumstantial", "supports", ["z"])]), link("e3", "c", "N3", [])] });
    expect(selectFrontier(g, cfg, 2)).toMatchObject({ priority: 3 });
  });
});
