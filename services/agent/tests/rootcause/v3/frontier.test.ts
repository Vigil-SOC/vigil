import { describe, expect, it } from "vitest";
import { createGraph } from "../../../plugins/rootcause/v3/graph.js";
import { isStalled } from "../../../plugins/rootcause/v3/frontier.js";
import { options } from "../../../plugins/rootcause/v3/lead.js";
import { apply, cfg, ev, node, seeded } from "./helpers.js";

const link = (tempId: string, source: string, target: string, evidence = [ev("direct", "supports", [`r${tempId}`])]) =>
  ({ tempId, kind: "explains" as const, source, target, prior: 0.3, evidence });
const solid = (tempId: string, over = {}) => node(tempId, { evidence: [ev("direct", "supports", [`r${tempId}`]), ev("corroborating", "supports", [`r${tempId}b`])], ...over });

// Which moves are open on which targets: the model picks among them, code only says what is available.
describe("options", () => {
  it("a lone symptom: strengthen and expand it, fork nothing (no cause to rival), branch nothing", () => {
    const g = createGraph("t");
    apply(g, { addNodes: [node("S", { isSymptom: true })] });
    expect(options(g, cfg)).toEqual({ expand: ["N1"], strengthen: ["N1"] });
  });
  it("an open boundary offers a pivot; an exhausted one does not", () => {
    const g = seeded();
    apply(g, { addNodes: [node("m", { kind: "boundary-marker", originNodeId: "N1", targetDomain: "email" })], addEdges: [{ ...link("e", "m", "N1"), evidence: [] }] });
    expect(options(g, cfg).pivot).toEqual(["B1"]);
    apply(g, { updateMarkers: [{ markerId: "B1", pivotSteps: cfg.maxForwardSteps }] });
    expect(options(g, cfg).pivot).toBeUndefined();
  });
  it("links, events and attributions are strengthen targets while unsettled; a boundary's placeholder link is not", () => {
    const g = seeded();
    apply(g, { addNodes: [node("a1", { kind: "actor", label: "x" }), solid("c"), node("m", { kind: "boundary-marker", originNodeId: "N2", targetDomain: "email" })],
      addEdges: [{ tempId: "x", kind: "attributed-to", source: "a1", target: "N1", prior: 0.3, evidence: [] }, link("e", "c", "N1", []), { ...link("me", "m", "N2"), evidence: [] }] });
    const s = options(g, cfg).strengthen!;
    expect(s).toEqual(expect.arrayContaining(["N1", "N2", "E1", "E2"]));
    expect(s).not.toContain("E3");
  });
  it("fork needs a cause to rival and a fork left", () => {
    const g = seeded();
    expect(options(g, cfg).fork).toBeUndefined();
    apply(g, { addNodes: [solid("c")], addEdges: [link("e", "c", "N1", [])] });
    expect(options(g, cfg).fork).toEqual(["N1"]);
    apply(g, { incrementForks: ["N1", "N1", "N1"] });
    expect(options(g, cfg).fork).toBeUndefined();
  });
  it("a stalled link leaves strengthen", () => {
    const g = seeded();
    apply(g, { addNodes: [solid("c")], addEdges: [link("e", "c", "N1", [])] });
    for (let t = 2; t <= 4; t++) apply(g, { incrementAttempts: ["E1"] }, t);
    expect(isStalled(g.edges.E1!, cfg)).toBe(true);
    expect(options(g, cfg).strengthen).not.toContain("E1");
  });
  it("stall by small movement over the last two attempts", () => {
    const g = seeded();
    apply(g, { addNodes: [node("c")] });
    apply(g, { incrementAttempts: ["N2"] }, 2);
    apply(g, { incrementAttempts: ["N2"] }, 3);
    expect(isStalled(g.nodes.N2!, cfg)).toBe(true);
    expect(options(g, cfg).strengthen).not.toContain("N2");
  });
  it("expand is capped per event, and a root is not expanded", () => {
    const g = seeded();
    apply(g, { addNodes: [solid("root", { isRoot: true })], addEdges: [link("e", "root", "N1", [])] });
    expect(options(g, cfg).expand).toEqual(["N1"]);
    for (let t = 2; t <= cfg.maxExpandAttempts + 2; t++) apply(g, { incrementExpand: ["N1"] }, t);
    expect(options(g, cfg).expand).toBeUndefined();
  });
  it("nothing is open once the graph is spent", () => {
    const g = seeded();
    apply(g, { setFlags: [{ nodeId: "N1", isRoot: true }], incrementExpand: ["N1", "N1", "N1"] });
    for (let t = 2; t <= 4; t++) apply(g, { incrementAttempts: ["N1"] }, t);
    expect(options(g, cfg)).toEqual({});
  });
});
