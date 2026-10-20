import { describe, expect, it } from "vitest";
import { check } from "../../../plugins/rootcause/v3/model/client.js";
import { cfg, ev, node, seeded } from "./helpers.js";
import { applyPatch, emptyPatch } from "../../../plugins/rootcause/v3/graph.js";

const cand = (label: string) => ({ label, domain: "edr", prior: 0.3, artifacts: [], nodeEvidence: [{ summary: "s", strength: "direct", polarity: "supports", toolCallIds: ["r1"] }], edgeEvidence: [] });

describe("loose intake, the only mode", () => {
  it("keeps a link with no shared value as inferred (circumstantial) instead of refusing it", () => {
    const g = seeded();
    applyPatch(g, { ...emptyPatch(), addNodes: [node("a", { evidence: [ev("direct")] })], addEdges: [{ tempId: "e", kind: "explains", source: "a", target: "N1", prior: 0.3, evidence: [ev("direct", "supports", ["r9"])] }] }, 1, { ...cfg, linkNeedsSharedValue: true });
    const e = Object.values(g.edges).find((x) => x.kind === "explains");
    expect(e?.inferred).toBe(true);
    expect(e?.evidence.every((v) => v.strength === "circumstantial")).toBe(true);
  });
  it("tags an accusing label with no baseline unverified instead of refusing the submission", () => {
    const r = check("submit_causes", { candidates: [cand("attacker runs xz on node-a")], reasoning: "r" }, 5);
    expect(r.success).toBe(true);
    expect((r as any).data.candidates[0].unverified).toBe(true);
  });
});
