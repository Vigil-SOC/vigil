import { describe, expect, it } from "vitest";
import { check } from "../../../plugins/rootcause/v3/model/client.js";
import { cfg, ev, node, seeded } from "./helpers.js";
import { applyPatch, emptyPatch } from "../../../plugins/rootcause/v3/graph.js";

const cand = (label: string) => ({ label, domain: "edr", prior: 0.3, artifacts: [], nodeEvidence: [{ summary: "s", strength: "direct", polarity: "supports", toolCallIds: ["tc_1"] }], edgeEvidence: [] });

describe("loose intake", () => {
  it("keeps a link with no shared value as inferred (circumstantial) instead of refusing it", () => {
    const strict = { ...cfg, linkNeedsSharedValue: true }, loose = { ...strict, loose: true };
    for (const [c, kept] of [[strict, false], [loose, true]] as const) {
      const g = seeded();
      applyPatch(g, { ...emptyPatch(), addNodes: [node("a", { evidence: [ev("direct")] })], addEdges: [{ tempId: "e", kind: "explains", source: "a", target: "N1", prior: 0.3, evidence: [ev("direct", "supports", ["tc_9"])] }] }, 1, c);
      const e = Object.values(g.edges).find((x) => x.kind === "explains");
      expect(!!e).toBe(kept);
      if (e) expect(e.inferred && e.evidence.every((v) => v.strength === "circumstantial")).toBe(true);
    }
  });
  it("tags an accusing label with no baseline unverified instead of refusing the submission", () => {
    const sub = { candidates: [cand("attacker runs xz on node-a")], reasoning: "r" };
    expect(check("submit_causes", sub, 5).success).toBe(false);
    const r = check("submit_causes", sub, 5, { loose: true });
    expect(r.success).toBe(true);
    expect((r as any).data.candidates[0].unverified).toBe(true);
  });
});
