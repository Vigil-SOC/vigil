import { describe, expect, it } from "vitest";
import { InvalidDecision, MAX_DECISION_ATTEMPTS, RULING_BATCH } from "../../workflows/hunt/controller.js";
import { buildReport } from "../../workflows/hunt/report.js";
import { evidenceStrength, unclassified } from "../../workflows/hunt/strength.js";
import { NOT_RULED, type Decision, type Digest, type EvidenceRelation } from "../../workflows/hunt/types.js";
import { bareEvidence, controllerFor, evidenceOn, INVESTIGATE, newLedger, relate, type Started } from "../support/hunt.js";

const NEW_OBSERVATIONS = 30;

// Earlier iterations' rulings on the board, then a fresh iteration of observations
// the lead has yet to rule on, against both hypotheses (the given one and the null).
async function wide(rows = NEW_OBSERVATIONS) {
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

// Rules every pair it is asked about except the first `skip`, as a lead that loses count would.
function ruling(started: Started, skip: number): Decision {
  const relations: EvidenceRelation[] = pending(started)
    .slice(skip)
    .map((pair) => ({ ...pair, relation: "supports" as const }));
  return { ...INVESTIGATE, evidence_relations: relations };
}

describe("an incomplete ruling does not fail the hunt", () => {
  it("carries a lead that misses rows twice, and fills what it never ruled", async () => {
    const { started, earlier, fresh } = await wide();
    const total = pending(started).length;
    expect(total).toBe(NEW_OBSERVATIONS * 2);

    // Refused twice for a few missed rows, then (the last attempt) a few missed again.
    const lead = [
      () => ruling(started, 4),
      (digest: Digest) => {
        const named = digest.notes.at(-1)!;
        expect(named).toContain("evidence_relations for only these");
        return { ...INVESTIGATE, evidence_relations: asked(started, named).slice(3).map(supports) };
      },
      (digest: Digest) => ({ ...INVESTIGATE, evidence_relations: asked(started, digest.notes.at(-1)!).slice(1).map(supports) }),
    ];
    const controller = controllerFor(started.ledger, lead);

    const result = await controller.advanceIteration();

    expect(result.hunt_status).toBe("active");
    const [decision] = started.ledger.projection.decisions;
    expect(decision!.rejected_attempts).toHaveLength(MAX_DECISION_ATTEMPTS);
    expect(pending(started)).toHaveLength(0);

    // Earlier supports and weakens stand, and the rows left unruled say so.
    const { links } = started.ledger.projection;
    expect(links.find((l) => l.evidence_id === earlier.supports)!.relation).toBe("supports");
    expect(links.find((l) => l.evidence_id === earlier.weakens)!.relation).toBe("weakens");
    const filled = links.filter((l) => l.note === NOT_RULED);
    expect(filled.length).toBeGreaterThan(0);
    expect(filled.every((l) => l.relation === "neither")).toBe(true);
    expect(links.filter((l) => l.relation === "supports" && fresh.includes(l.evidence_id)).length).toBeGreaterThan(total - 10);
  });

  it("asks only for the pairs still unruled, in a bounded batch", async () => {
    const { started } = await wide();
    const total = pending(started).length;
    const controller = controllerFor(started.ledger, [() => ruling(started, total - 1), () => ruling(started, 0)]);

    // The first emission rules one pair; the note must name a batch, not all 59.
    await controller.advanceIteration();
    expect(started.ledger.projection.decisions[0]!.rejected_attempts).toHaveLength(1);
    const note = started.ledger.projection.decisions[0]!.digest_presented.notes.join("\n");
    const named = [...note.matchAll(/ev-[\w-]+/g)].length;
    expect(named).toBeLessThanOrEqual(RULING_BATCH);
    expect(note).toContain(`of ${total - 1}`);
  });

  it("proceeds on the fallback when coverage never completes", async () => {
    const { started, earlier } = await wide();
    const controller = controllerFor(started.ledger, [INVESTIGATE, INVESTIGATE, INVESTIGATE]);

    const result = await controller.advanceIteration();

    expect(result.hunt_status).toBe("active");
    const { links } = started.ledger.projection;
    expect(links.filter((l) => l.note === NOT_RULED)).toHaveLength(NEW_OBSERVATIONS * 2);
    expect(links.find((l) => l.evidence_id === earlier.supports)!.relation).toBe("supports");
    // The filled rows are reported as unruled; a real neither would not be.
    expect(buildReport(started.ledger.projection).unruled).toBe(NEW_OBSERVATIONS);
  });

  it("does not ask about rows the digest dropped, and records them as not ruled", async () => {
    const { started, fresh } = await wide(45);
    const lead = (digest: Digest): Decision => {
      const shown = new Set(digest.recent_evidence.map((e) => e.evidence_id));
      return {
        ...INVESTIGATE,
        evidence_relations: pending(started).filter((p) => shown.has(p.evidence_id)).map(supports),
      };
    };
    await controllerFor(started.ledger, [lead]).advanceIteration();

    const [decision] = started.ledger.projection.decisions;
    // One refusal for the omitted rows is enough: the lead was never asked to rule on them.
    expect(decision!.rejected_attempts).toHaveLength(1);
    const shown = new Set(decision!.digest_presented.recent_evidence.map((e) => e.evidence_id));
    const dropped = fresh.filter((id) => !shown.has(id));
    expect(dropped.length).toBeGreaterThan(0);
    const filled = started.ledger.projection.links.filter((l) => l.note === NOT_RULED);
    expect(filled).toHaveLength(dropped.length * 2);
    expect(filled.every((l) => dropped.includes(l.evidence_id))).toBe(true);
  });

  it("still stalls a decision that is invalid for another reason too", async () => {
    const { started } = await wide();
    const bad = { ...INVESTIGATE, target_hypothesis_id: "h-nope" };
    await expect(controllerFor(started.ledger, [bad, bad, bad]).advanceIteration()).rejects.toThrow(InvalidDecision);
  });
});

describe("rulings are permanent", () => {
  it("keeps every link and count through a stalled iteration and the terminal fold", async () => {
    const { started, given, benign } = await wide(3);
    for (const pair of pending(started)) relate(started.ledger, pair.evidence_id, pair.hypothesis_id, "supports");
    const before = {
      links: structuredClone(started.ledger.projection.links),
      given: evidenceStrength(started.ledger.projection, given),
      benign: evidenceStrength(started.ledger.projection, benign),
    };

    const stall = { ...INVESTIGATE, target_hypothesis_id: "h-nope" };
    const controller = controllerFor(started.ledger, [stall, stall, stall]);
    await expect(controller.advanceIteration()).rejects.toThrow(InvalidDecision);
    controller.terminate("failed", "the lead could not decide");

    const { projection } = started.ledger;
    expect(projection.links).toEqual(before.links);
    expect(evidenceStrength(projection, given)).toEqual(before.given);
    expect(evidenceStrength(projection, benign)).toEqual(before.benign);
  });

  it("does not let a neither undo a recorded supports or weakens", async () => {
    const { started, given, earlier } = await wide(1);
    const [row] = pending(started);
    relate(started.ledger, row!.evidence_id, row!.hypothesis_id, "weakens");
    const rule = (id: string, hypothesisId: string, relation: "neither" | "supports"): EvidenceRelation => ({
      evidence_id: id,
      hypothesis_id: hypothesisId,
      relation,
    });

    const lead = {
      ...INVESTIGATE,
      evidence_relations: [
        ...pending(started).map((p) => rule(p.evidence_id, p.hypothesis_id, "neither")),
        rule(earlier.supports, given, "neither"),
        rule(row!.evidence_id, row!.hypothesis_id, "neither"),
      ],
    };
    await controllerFor(started.ledger, [lead]).advanceIteration();

    const { links } = started.ledger.projection;
    expect(links.find((l) => l.evidence_id === earlier.supports && l.hypothesis_id === given)!.relation).toBe("supports");
    expect(links.find((l) => l.evidence_id === row!.evidence_id && l.hypothesis_id === row!.hypothesis_id)!.relation).toBe("weakens");
  });
});

const supports = (pair: { evidence_id: string; hypothesis_id: string }): EvidenceRelation => ({ ...pair, relation: "supports" });

// The pairs a re-ask note names, read back from its "evidence: hypotheses; ..." list.
function asked(started: Started, note: string): { evidence_id: string; hypothesis_id: string }[] {
  const list = note.slice(note.lastIndexOf("neither: ") + 9);
  return list
    .replace(/\.$/, "")
    .split("; ")
    .flatMap((entry) => {
      const [evidenceId, hypotheses] = entry.split(": ") as [string, string];
      return hypotheses.split(", ").map((hypothesis_id) => ({ evidence_id: evidenceId, hypothesis_id }));
    })
    .filter((pair) => pending(started).some((p) => p.evidence_id === pair.evidence_id && p.hypothesis_id === pair.hypothesis_id));
}
