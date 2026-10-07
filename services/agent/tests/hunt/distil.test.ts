import { describe, expect, it } from "vitest";
import { DISTIL_SCHEMA_VERSION, huntDistil } from "../../workflows/hunt/distil.js";
import { NULL_CHECK_PROVENANCE } from "../../workflows/hunt/strength.js";
import { evidenceOn, newLedger, relate } from "../support/hunt.js";

describe("huntDistil carries the techniques the evidence cited", () => {
  it("puts a cited T-ID on the hypothesis it bore on and not on one with no bearing evidence", async () => {
    const { ledger, runId, hypothesisIds } = await newLedger({
      hypotheses: ["beaconing over web protocols", "a credential is used from new infrastructure"],
    });
    const [beaconing, credential] = hypothesisIds as [string, string];
    evidenceOn(ledger, beaconing, { source: "net_flow", attackTechnique: "T1071.001" });
    evidenceOn(ledger, beaconing, { source: "dns", attackTechnique: "T1071.001" });
    evidenceOn(ledger, credential, { source: "cloudtrail" });

    const payload = huntDistil(runId, ledger.log);
    const byId = new Map(payload.conclusions.map((conclusion) => [conclusion.hypothesis_id, conclusion]));

    expect(payload.distil_schema_version).toBe(DISTIL_SCHEMA_VERSION);
    expect(byId.get(beaconing)!.techniques).toEqual(["T1071.001"]);
    expect(byId.get(credential)!.techniques).toEqual([]);
  });

  it("counts a `weakens` citation and ignores `neither` links and the harness's own records", async () => {
    const { ledger, runId, hypothesisIds } = await newLedger();
    const hypothesisId = hypothesisIds[0]!;
    evidenceOn(ledger, hypothesisId, { source: "dns", relation: "neither", attackTechnique: "T1568" });
    evidenceOn(ledger, hypothesisId, { source: "endpoint", relation: "weakens", attackTechnique: "T1059.001" });

    // A failed dispatch and the critic's benign case are the harness talking,
    // and must not reach a Verdict even when a link says they bear on the claim.
    const harness = (evidenceId: string, source_system: string, provenance: string) =>
      ledger.append({
        kind: "evidence",
        payload: {
          evidence_id: evidenceId,
          dispatch_id: null,
          iteration: 1,
          source_system,
          summary: `${source_system} spoke`,
          payload: {},
          salience: "routine",
          why_notable: "",
          provenance,
          attacker_influenceable: false,
          instruction_like: false,
          entities: [],
          captured_at: new Date().toISOString(),
          attack_technique: "T1071.001",
        },
      });
    harness("ev-failed", "dispatcher", "tool_failure");
    relate(ledger, "ev-failed", hypothesisId, "supports");
    harness("ev-critic", "critic", NULL_CHECK_PROVENANCE);
    relate(ledger, "ev-critic", hypothesisId, "weakens");

    const [conclusion] = huntDistil(runId, ledger.log).conclusions;
    expect(conclusion!.techniques).toEqual(["T1059.001"]);
  });
});
