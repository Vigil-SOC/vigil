import { describe, expect, it } from "vitest";
import { check } from "../../../plugins/rootcause/v3/model/client.js";

const cand = (label: string, extra: object = {}) => ({ label, domain: "edr", prior: 0.3, artifacts: [], nodeEvidence: [{ summary: "s", strength: "direct", polarity: "supports", toolCallIds: ["tc_1"] }], edgeEvidence: [], ...extra });
const base = { baseline: { summary: "first time from this address", unusual: true, toolCallIds: ["tc_2"] } };

describe("attribution: a takeover claim needs the takeover record", () => {
  it("refuses 'stolen credential' with no takeover record, in loose mode too", () => {
    const r = check("submit_causes", { candidates: [cand("Attacker using stolen admin credential ran kubectl", base)], reasoning: "r" }, 5, { loose: true });
    expect(r.success).toBe(false);
    expect((r as { msg: string }).msg).toMatch(/no takeover record/);
  });
  it("refuses a takeover record that describes a missing record", () => {
    const r = check("submit_causes", { candidates: [cand("admin credential compromised", { ...base, takeover: { summary: "no AssumeRole or login record found for this session", toolCallIds: ["tc_3"] } })], reasoning: "r" }, 5);
    expect((r as { msg: string }).msg).toMatch(/describes a missing one/);
  });
  it("accepts a takeover claim citing the takeover record", () => {
    const r = check("submit_causes", { candidates: [cand("alice's session used by the attacker's implant", { ...base, takeover: { summary: "rundll32 implant process running as alice", toolCallIds: ["tc_3"] } })], reasoning: "r" }, 5);
    expect(r.success).toBe(true);
  });
  it("leaves neutral labels alone", () => {
    expect(check("submit_causes", { candidates: [cand("admin from 1.2.3.4 patched the deployment")], reasoning: "r" }, 5).success).toBe(true);
  });
  it("a story naming a takeover or an insider must predict the takeover record", () => {
    const story = (s: string, expect: string) => ({ theories: [
      { id: "H1", story: s, accountsFor: [], gaps: "g", predictions: [{ id: "P1", sourcetype: "x", expect, spl: "index=x", separates: [] }] },
      { id: "H2", story: "routine work", accountsFor: [], gaps: "g", predictions: [{ id: "P2", sourcetype: "x", expect: "y", spl: "index=x", separates: [] }] },
    ], nextTests: ["P1"], reasoning: "r" });
    expect((check("submit_theories", story("The developer is a rogue insider who deployed it", "a deploy record"), 5) as { msg: string }).msg).toMatch(/H1: the story says/);
    expect(check("submit_theories", story("The developer's account was compromised", "a sign-in to the developer's account from a new address"), 5).success).toBe(true);
  });
});
