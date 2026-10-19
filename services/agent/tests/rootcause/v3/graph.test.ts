import { describe, expect, it } from "vitest";
import { bestPathFromSymptom, createGraph } from "../../../plugins/rootcause/v3/graph.js";
import { apply, ev, node, seeded } from "./helpers.js";

describe("graph", () => {
  it("maps tempIds and links a cause", () => {
    const g = seeded();
    const r = apply(g, { addNodes: [node("c")], addEdges: [{ tempId: "e", kind: "explains", source: "c", target: "N1", prior: 0.3, evidence: [] }] });
    expect(r.idMap.c).toBe("N2");
    expect(g.edges[r.idMap.e].source).toBe("N2");
    expect(bestPathFromSymptom(g)!.nodeIds).toEqual(["N1", "N2"]);
  });
  it("rejects cycles", () => {
    const g = seeded();
    apply(g, { addNodes: [node("c")], addEdges: [{ tempId: "e", kind: "explains", source: "c", target: "N1", prior: 0.3, evidence: [] }] });
    const r = apply(g, { addEdges: [{ tempId: "x", kind: "explains", source: "N1", target: "N2", prior: 0.3, evidence: [] }] });
    expect(r.rejected[0].reason).toMatch(/cycle/);
  });
  it("dedups on a strong artifact", () => {
    const g = seeded();
    apply(g, { addNodes: [node("a", { label: "ran x", artifacts: [{ kind: "event-id", value: "42" }] })] });
    const r = apply(g, { addNodes: [node("b", { label: "process x started", artifacts: [{ kind: "event-id", value: "42" }], evidence: [ev("corroborating")] })] });
    expect(r.idMap.b).toBe("N2");
    expect(Object.keys(g.nodes)).toHaveLength(2);
    expect(g.nodes.N2.evidence).toHaveLength(1);
  });
  it("no resurrection of ruled-out nodes without new direct evidence", () => {
    const g = seeded();
    apply(g, { addNodes: [node("a", { label: "red herring", evidence: [ev("direct", "refutes")] })] });
    expect(g.nodes.N2.status).toBe("ruled-out");
    expect(apply(g, { addNodes: [node("b", { label: "red herring" })] }).rejected).toHaveLength(1);
    expect(apply(g, { addNodes: [node("c", { label: "red herring", evidence: [ev("direct", "supports", ["tc_2"]), ev("direct", "supports", ["tc_3"])] })] }).idMap.c).toBe("N2");
  });
  it("recomputes disputed", () => {
    const g = seeded();
    apply(g, { addNodes: [node("a"), node("b")], addEdges: [
      { tempId: "e1", kind: "explains", source: "a", target: "N1", prior: 0.3, evidence: [] },
      { tempId: "e2", kind: "explains", source: "b", target: "N1", prior: 0.3, evidence: [] }] });
    expect(g.edges.E1.disputed && g.edges.E2.disputed).toBe(true);
    apply(g, { addEvidence: [{ targetId: "E2", evidence: ev("direct", "refutes", ["tc_9"]) }] });
    expect(g.edges.E2.status).toBe("ruled-out");
    expect(g.edges.E1.disputed).toBe(false);
  });
  it("auto-resolves a marker when its tentative stitch edge confirms", () => {
    const g = seeded();
    apply(g, { addNodes: [node("m", { kind: "boundary-marker", label: "upstream", domain: "email", originNodeId: "N1", targetDomain: "email" }), node("c", { domain: "email" })],
      addEdges: [{ tempId: "me", kind: "explains", source: "m", target: "N1", prior: 0.3, evidence: [] },
        { tempId: "ce", kind: "explains", source: "c", target: "N1", prior: 0.3, evidence: [ev("corroborating")], stitchFor: "B1" }] });
    expect(g.nodes.B1.resolvedTo).toBeUndefined();
    const r = apply(g, { addEvidence: [{ targetId: "E2", evidence: ev("direct", "supports", ["tc_7"]) }] });
    expect(r.resolvedMarkers).toEqual(["B1"]);
    expect(g.edges.E1.retired).toBe(true);
  });
  it("survives a JSON round trip", () => {
    const g = seeded();
    expect(JSON.parse(JSON.stringify(g))).toEqual(g);
    expect(createGraph("x").nodes).toEqual({});
  });
  it("records attempt confidence before new evidence", () => {
    const g = seeded();
    apply(g, { addNodes: [node("a")] });
    apply(g, { incrementAttempts: ["N2"], addEvidence: [{ targetId: "N2", evidence: ev("direct") }] }, 2);
    expect(g.nodes.N2.attemptConf[0]).toBeCloseTo(0.3, 9);
    expect(g.nodes.N2.strengthenAttempts).toBe(1);
  });
});
