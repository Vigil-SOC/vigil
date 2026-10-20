import { describe, expect, it } from "vitest";
import { snapshot } from "../../../plugins/rootcause/v3/model/snapshot.js";
import { apply, ev, node, seeded } from "./helpers.js";

describe("snapshot", () => {
  it("is stable, lists ruled-out hypotheses, caps evidence", () => {
    const g = seeded();
    apply(g, {
      addNodes: [node("c", { label: "cause", evidence: [ev("circumstantial", "supports", ["a"]), ev("circumstantial", "supports", ["b"]), ev("corroborating", "supports", ["c"])] }), node("x", { label: "red herring", evidence: [ev("direct", "refutes", ["d"])] })],
      addEdges: [{ tempId: "e", kind: "explains", source: "c", target: "N1", prior: 0.3, evidence: [] }],
    });
    const s = snapshot(g);
    expect(s).toBe(snapshot(g));
    expect(s).toMatch(/RULED OUT\nN3 red herring/);
    expect(s.match(/^ {3}· /gm)).toHaveLength(3); // 1 on N1 + 2 of N2's 3
    expect(s).toMatch(/E1 N2 -explains-> N1 0\.30/);
    expect(s).toMatchInlineSnapshot(`
      "<graph_snapshot>
      NODES
      N1 [action|confirmed|0.88|edr] symptom SYMPTOM PATH
         · +direct [rs]: direct supports
      N2 [action|suspected|0.72|edr] cause PATH
         · +circumstantial [b]: circumstantial supports
         · +corroborating [c]: corroborating supports
      EDGES
      E1 N2 -explains-> N1 0.30
      RULED OUT
      N3 red herring (direct refutes)
      </graph_snapshot>"
    `);
  });
});
