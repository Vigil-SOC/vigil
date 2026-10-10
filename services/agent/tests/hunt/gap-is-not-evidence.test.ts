// A gap is not evidence (#2201). A row recording a query that timed out or came back
// empty was ruled "weakens" on a rehearsal with no SIEM, so a hypothesis went Standing
// to Weakened to Inconclusive with nothing ever found against it.
import { describe, expect, it } from "vitest";
import { evidenceFrom } from "../../workflows/hunt/adapters.js";
import { evidenceStrength, isGap, openGaps, unclassified } from "../../workflows/hunt/strength.js";
import { bareEvidence, bookOf, evidenceOn, newLedger, relate } from "../support/hunt.js";

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

describe("a timed-out query is a blind spot, not a ruling", () => {
  it("keeps the hypothesis's status and counts, and gains one blind spot", async () => {
    const { ledger, hypothesisIds } = await loop();
    const hypothesisId = hypothesisIds[0]!;
    // Ruled on the benign account too, so every record on the board has a ruling.
    const benign = [...ledger.projection.hypotheses.keys()].find((id) => id !== hypothesisId)!;
    for (const source of ["cloudtrail", "duckdb"]) {
      relate(ledger, evidenceOn(ledger, hypothesisId, { source }), benign, "neither");
    }
    const before = evidenceStrength(ledger.projection, hypothesisId);
    const gapsBefore = [...ledger.projection.evidence.values()].filter(isGap).length;

    // The agent even records it as weakening the hypothesis; the record does not take it.
    bookOf(ledger).appendEvidence(timedOut({ weakens: [hypothesisId] }));

    const after = evidenceStrength(ledger.projection, hypothesisId);
    const links = ledger.projection.links.filter((link) => link.hypothesis_id === hypothesisId);

    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
    expect(links.filter((link) => link.relation === "supports")).toHaveLength(2);
    expect(links.filter((link) => link.relation === "weakens")).toHaveLength(0);
    expect(after.corroborating_sources).toBe(before.corroborating_sources);
    expect(after.contradicting_records).toBe(before.contradicting_records);
    expect([...ledger.projection.evidence.values()].filter(isGap)).toHaveLength(gapsBefore + 1);
    // Nothing left to rule on, so the gap asks no ruling of the agent.
    expect(unclassified(ledger.projection)).toHaveLength(0);
  });

  it("counts against the hypothesis the row was recorded for, and no other", async () => {
    const { ledger, hypothesisIds } = await loop();
    const [served, other] = [...ledger.projection.hypotheses.keys()];
    bookOf(ledger).appendEvidence(timedOut({ query_failed: true, supports: [served!] }));

    expect(openGaps(ledger.projection, hypothesisIds[0]!)).toBe(1);
    expect(openGaps(ledger.projection, other!)).toBe(0);
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

describe("the record refuses to rule on a gap", () => {
  it("skips supports and weakens on a row that records a failed query, and takes neither", async () => {
    const { ledger, hypothesisIds } = await loop();
    const hypothesisId = hypothesisIds[0]!;
    const evidenceId = bareEvidence(ledger);
    const row = ledger.projection.evidence.get(evidenceId)!;
    ledger.append({ kind: "evidence", payload: { ...row, evidence_id: "ev-gap", provenance: "tool_failure" } });
    const book = bookOf(ledger);
    const rule = (relation: "supports" | "weakens" | "neither") => book.rule([{ evidence_id: "ev-gap", hypothesis_id: hypothesisId, relation }]);

    const weakens = rule("weakens");
    expect(weakens.ruled).toBe(0);
    expect(weakens.skipped[0]).toMatch(/blind spot/);
    expect(rule("supports").skipped[0]).toMatch(/blind spot/);
    expect(rule("neither")).toEqual({ ruled: 1, skipped: [] });
    expect(ledger.projection.links.filter((link) => link.evidence_id === "ev-gap").map((link) => link.relation)).toEqual(["neither"]);
  });
});
