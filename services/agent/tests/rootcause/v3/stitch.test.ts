import { describe, expect, it } from "vitest";
import { matchBoundary } from "../../../plugins/rootcause/v3/stitch.js";
import { cfg } from "./helpers.js";

const origin = { label: "excel opened invoice", observedAt: "2026-04-02T10:00:00Z", artifacts: [{ kind: "file-hash" as const, value: "AB12" }, { kind: "ip" as const, value: "10.0.0.5" }] };
const marker = { id: "B1" };
const cand = (over: object) => ({ label: "email delivered", domain: "email", observedAt: "2026-04-02T09:50:00Z", artifacts: [], ...over });

describe("stitch", () => {
  it("auto on a shared strong artifact", () => {
    expect(matchBoundary(cand({ artifacts: [{ kind: "file-hash", value: "ab12" }] }), marker, origin, cfg, 1).kind).toBe("auto");
  });
  it("tentative on a medium artifact", () => {
    const m = matchBoundary(cand({ observedAt: "2026-04-01T09:00:00Z", artifacts: [{ kind: "ip", value: "10.0.0.5" }] }), marker, origin, cfg, 1);
    expect(m.kind).toBe("tentative");
    expect(m.joinEvidence.map((e) => e.strength)).toEqual(["corroborating"]);
  });
  it("none when nothing is shared and it is long before", () => {
    expect(matchBoundary(cand({ observedAt: "2026-04-01T09:00:00Z" }), marker, origin, cfg, 1).kind).toBe("none");
  });
  it("disqualified when the candidate is after the origin", () => {
    expect(matchBoundary(cand({ observedAt: "2026-04-02T10:05:00Z", artifacts: [{ kind: "file-hash", value: "AB12" }] }), marker, origin, cfg, 1).kind).toBe("none");
  });
});
