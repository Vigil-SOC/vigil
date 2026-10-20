import { describe, expect, it } from "vitest";
import { MemoryTrace } from "../../../plugins/rootcause/v3/trace.js";

describe("trace", () => {
  it("byId matches correlation and related ids; chronological order", () => {
    let t = 0;
    const tr = new MemoryTrace("r", () => new Date(Date.UTC(2026, 0, 1, 0, 0, t++)));
    const a = tr.forTurn(1, "expand", "N1");
    a.write("decision", {});
    a.write("graph-patch", {});
    a.relate(["N2", "E1"]);
    tr.forTurn(2, "strengthen", "E1").write("decision", {});
    expect(tr.byId("N2")).toHaveLength(2);
    expect(tr.byId("E1")).toHaveLength(3);
    expect(tr.chronological().map((e) => e.turn)).toEqual([1, 1, 2]);
  });
});
