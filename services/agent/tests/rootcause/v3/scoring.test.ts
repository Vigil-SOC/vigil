import { describe, expect, it } from "vitest";
import { confidence, status, vetEvidence, clampPrior } from "../../../plugins/rootcause/v3/scoring.js";
import type { Evidence } from "../../../plugins/rootcause/v3/types.js";
import { cfg } from "./helpers.js";

const e = (strength: Evidence["strength"], polarity: Evidence["polarity"] = "supports", toolCallIds = ["tc_1"]): Evidence =>
  ({ id: "V", source: "x", summary: "", strength, polarity, toolCallIds, traceIds: [], turn: 1 });

describe("scoring", () => {
  it("worked example: 0.3 -> direct 0.76 -> corroborating 0.90", () => {
    expect(confidence(0.3, [e("direct")], cfg)).toBeCloseTo(0.76, 2);
    expect(confidence(0.3, [e("direct", "supports", ["a"]), e("corroborating", "supports", ["b"])], cfg)).toBeCloseTo(0.9, 2);
  });
  it("status thresholds", () => {
    expect(status(0.9, [e("direct")], cfg)).toBe("confirmed");
    expect(status(0.9, [e("corroborating")], cfg)).toBe("suspected"); // no direct support
    expect(status(0.08, [], cfg)).toBe("ruled-out");
    expect(status(0.5, [], cfg)).toBe("suspected");
    expect(status(0.9, [e("direct")], cfg, true)).toBe("unknown");
  });
  it("uncited evidence is downgraded, unknown tool call rejected", () => {
    const calls = new Set(["tc_1"]);
    const up = vetEvidence({ summary: "", strength: "direct", polarity: "supports", toolCallIds: [] }, calls);
    expect(up.ok && up.value.strength).toBe("circumstantial");
    expect(vetEvidence({ summary: "", strength: "direct", polarity: "supports", toolCallIds: ["tc_9"] }, calls).ok).toBe(false);
  });
  it("duplicate evidence counts once", () => {
    expect(confidence(0.3, [e("direct"), e("direct")], cfg)).toBeCloseTo(confidence(0.3, [e("direct")], cfg), 6);
  });
  it("prior clamped", () => {
    expect(clampPrior(0.9, cfg)).toBe(0.5);
    expect(clampPrior(0, cfg)).toBe(0.1);
  });
});
