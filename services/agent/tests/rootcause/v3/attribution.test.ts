import { describe, expect, it } from "vitest";
import { buildAttributionPatch } from "../../../plugins/rootcause/v3/attribution.js";
import { apply, ev, node, seeded } from "./helpers.js";

const ctx = { turn: 1, domain: "git", traceIds: () => [] };

describe("attribution", () => {
  it("classic divergence: spoofed name ruled out (0.08), system identity confirmed (0.95)", () => {
    const g = seeded();
    apply(g, { addNodes: [node("act", { label: "commit pushed", evidence: [ev("direct")] })] });
    const p = buildAttributionPatch("N2", [
      { layer: "human-facing", kind: "commit-author", value: "Dev B <dev-b@corp>", resolvesTo: "git:dev-b", toolCallIds: ["tc_1"] },
      { layer: "machine-facing", kind: "actor-login", value: "fork-x", resolvesTo: "git:fork-x", toolCallIds: ["tc_2"] },
    ], g, ctx);
    apply(g, p);
    const edges = Object.values(g.edges);
    const legit = edges.find((e) => g.nodes[e.source].label === "git:dev-b")!;
    const attacker = edges.find((e) => g.nodes[e.source].label === "git:fork-x")!;
    expect(legit.confidence).toBeCloseTo(0.08, 2);
    expect(legit.status).toBe("ruled-out");
    expect(attacker.confidence).toBeCloseTo(0.95, 2);
    expect(attacker.status).toBe("confirmed");
  });
  it("conflicting machine signals stay disputed", () => {
    const g = seeded();
    apply(g, { addNodes: [node("act")] });
    apply(g, buildAttributionPatch("N2", [
      { layer: "machine-facing", kind: "logon-id", value: "a", resolvesTo: "ad:a", toolCallIds: ["tc_1"] },
      { layer: "machine-facing", kind: "logon-id", value: "b", resolvesTo: "ad:b", toolCallIds: ["tc_2"] },
    ], g, ctx));
    const edges = Object.values(g.edges);
    expect(edges.every((e) => e.disputed && e.status === "suspected")).toBe(true);
  });
  it("reuses an existing actor", () => {
    const g = seeded();
    apply(g, { addNodes: [node("a1"), node("a2")] });
    apply(g, buildAttributionPatch("N2", [{ layer: "machine-facing", kind: "sid", value: "S-1", resolvesTo: "ad:bob", toolCallIds: ["tc_1"] }], g, ctx));
    apply(g, buildAttributionPatch("N3", [{ layer: "machine-facing", kind: "sid", value: "S-1", resolvesTo: "ad:bob", toolCallIds: ["tc_1"] }], g, ctx));
    expect(Object.values(g.nodes).filter((n) => n.kind === "actor")).toHaveLength(1);
  });
});
