import { describe, expect, it } from "vitest";
import { evidenceStrength, unclassified } from "../../workflows/hunt/strength.js";
import { bareEvidence, bookOf, evidenceOn, newLedger, relate, type Started } from "../support/hunt.js";

// The controller's re-asks for an incomplete ruling, and its NOT_RULED fill, went
// with it: rule_evidence takes what the agent rules, and the report counts the rest.

// Earlier rulings on the board, then fresh observations nobody has ruled on yet,
// against both hypotheses (the given one and the null).
async function wide(rows: number) {
  const started = await newLedger({ hypothesisLoop: true });
  const [given, benign] = [...started.ledger.projection.hypotheses.keys()] as [string, string];
  const earlier = {
    supports: evidenceOn(started.ledger, given, { relation: "supports" }),
    weakens: evidenceOn(started.ledger, benign, { relation: "weakens" }),
  };
  const fresh = Array.from({ length: rows }, () => bareEvidence(started.ledger, "duckdb", 2));
  return { started, given, benign, earlier, fresh };
}

const pending = (started: Started) => unclassified(started.ledger.projection);

describe("rulings are permanent", () => {
  it("keeps every link and count through the terminal fold", async () => {
    const { started, given, benign } = await wide(3);
    for (const pair of pending(started)) relate(started.ledger, pair.evidence_id, pair.hypothesis_id, "supports");
    const before = {
      links: structuredClone(started.ledger.projection.links),
      given: evidenceStrength(started.ledger.projection, given),
      benign: evidenceStrength(started.ledger.projection, benign),
    };

    bookOf(started.ledger).terminate("failed", "the agent could not go on");

    const { projection } = started.ledger;
    expect(projection.links).toEqual(before.links);
    expect(evidenceStrength(projection, given)).toEqual(before.given);
    expect(evidenceStrength(projection, benign)).toEqual(before.benign);
  });

  it("does not let a neither undo a recorded supports or weakens", async () => {
    const { started, given, earlier } = await wide(1);
    const [row] = pending(started);
    relate(started.ledger, row!.evidence_id, row!.hypothesis_id, "weakens");

    bookOf(started.ledger).rule([
      ...pending(started).map((p) => ({ ...p, relation: "neither" as const })),
      { evidence_id: earlier.supports, hypothesis_id: given, relation: "neither" },
      { evidence_id: row!.evidence_id, hypothesis_id: row!.hypothesis_id, relation: "neither" },
    ]);

    const { links } = started.ledger.projection;
    const on = (evidenceId: string, hypothesisId: string) => links.filter((l) => l.evidence_id === evidenceId && l.hypothesis_id === hypothesisId);
    // Not overwritten, and no neither stacked beside it either.
    expect(on(earlier.supports, given).map((l) => l.relation)).toEqual(["supports"]);
    expect(on(row!.evidence_id, row!.hypothesis_id).map((l) => l.relation)).toEqual(["weakens"]);
    expect(pending(started)).toHaveLength(0);
  });
});
