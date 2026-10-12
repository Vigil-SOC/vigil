// A gap is not evidence (#2201). A row recording a query that timed out or came back
// empty was ruled "weakens" on a rehearsal with no SIEM, so a hypothesis went Standing
// to Weakened to Inconclusive with nothing ever found against it.
import { describe, expect, it } from "vitest";
import { evidenceFrom } from "../../workflows/hunt/adapters.js";
import { InvalidDecision, validateDecision } from "../../workflows/hunt/controller.js";
import { evidenceStrength, isGap, openGaps, unclassified } from "../../workflows/hunt/strength.js";
import type { Decision } from "../../workflows/hunt/types.js";
import { bareEvidence, controllerFor, evidenceOn, INVESTIGATE, newLedger, relate } from "../support/hunt.js";

const loop = () => newLedger({ hypothesisLoop: true });

function timedOut(extra: Record<string, unknown> = {}) {
  return evidenceFrom({
    results: [
      {
        source_system: "okta",
        summary: "Raw Okta records for 185.220.101.47 were not retrieved: the query timed out",
        salience: "routine",
        why_notable: "nothing came back",
        payload: {},
        ...extra,
      },
    ],
  });
}

function answeringWith(evidence: ReturnType<typeof timedOut>) {
  return {
    dispatch: async (request: { dispatch_id: string }) => ({
      dispatch_id: request.dispatch_id,
      evidence,
      failed: false,
      failure_reason: "",
      cost_usd: 0,
    }),
  } as never;
}

describe("a timed-out query is a blind spot, not a ruling", () => {
  it("keeps the hypothesis's status and counts, and gains one blind spot", async () => {
    const { ledger, hypothesisIds } = await loop();
    const hypothesisId = hypothesisIds[0]!;
    // Ruled on the benign account too, as the lead must before it may decide anything.
    const benign = [...ledger.projection.hypotheses.keys()].find((id) => id !== hypothesisId)!;
    for (const source of ["cloudtrail", "duckdb"]) {
      relate(ledger, evidenceOn(ledger, hypothesisId, { source }), benign, "neither");
    }
    const before = evidenceStrength(ledger.projection, hypothesisId);
    const gapsBefore = [...ledger.projection.evidence.values()].filter(isGap).length;

    // The worker even claims it weakens the hypothesis; the controller does not take it.
    const controller = controllerFor(ledger, [{ ...INVESTIGATE, target_hypothesis_id: hypothesisId }], {
      dispatcher: answeringWith(timedOut({ weakens: [hypothesisId] })),
    });
    await controller.advanceIteration();

    const after = evidenceStrength(ledger.projection, hypothesisId);
    const links = ledger.projection.links.filter((link) => link.hypothesis_id === hypothesisId);

    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
    expect(links.filter((link) => link.relation === "supports")).toHaveLength(2);
    expect(links.filter((link) => link.relation === "weakens")).toHaveLength(0);
    expect(after.corroborating_sources).toBe(before.corroborating_sources);
    expect(after.contradicting_records).toBe(before.contradicting_records);
    expect([...ledger.projection.evidence.values()].filter(isGap)).toHaveLength(gapsBefore + 1);
    // Nothing for the lead to rule on, so the next decision is not refused over it.
    expect(unclassified(ledger.projection)).toHaveLength(0);
  });

  it("counts against the hypothesis the dispatch served", async () => {
    const { ledger, hypothesisIds } = await loop();
    const controller = controllerFor(ledger, [{ ...INVESTIGATE, target_hypothesis_id: hypothesisIds[0]! }], {
      dispatcher: answeringWith(timedOut({ query_failed: true })),
    });
    await controller.advanceIteration();

    expect(openGaps(ledger.projection, hypothesisIds[0]!)).toBe(1);
  });

  it("takes the worker's flag as well as its wording", () => {
    const [flagged] = evidenceFrom({
      results: [
        { source_system: "okta", summary: "nothing to report", salience: "routine", why_notable: "-", payload: {}, query_failed: true },
      ],
    });
    expect(flagged!.query_failed).toBe(true);
  });
});

describe("the controller refuses to rule on a gap", () => {
  it("rejects supports and weakens on a row that records a failed query, and allows neither", async () => {
    const { ledger, hypothesisIds } = await loop();
    const hypothesisId = hypothesisIds[0]!;
    const evidenceId = bareEvidence(ledger);
    const row = ledger.projection.evidence.get(evidenceId)!;
    ledger.append({ kind: "evidence", payload: { ...row, evidence_id: "ev-gap", provenance: "tool_failure" } });
    relate(ledger, "ev-gap", hypothesisId, "neither");

    const rule = (relation: "supports" | "weakens" | "neither"): Decision => ({
      ...INVESTIGATE,
      evidence_relations: [{ evidence_id: "ev-gap", hypothesis_id: hypothesisId, relation }],
    });

    expect(() => validateDecision(rule("weakens"), ledger.projection)).toThrow(InvalidDecision);
    expect(() => validateDecision(rule("supports"), ledger.projection)).toThrow(/blind spot/);
    expect(() => validateDecision(rule("neither"), ledger.projection)).not.toThrow(/blind spot/);
  });
});
