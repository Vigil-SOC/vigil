import { describe, expect, it } from "vitest";
import {
  DEFAULT_VERDICTS,
  validateThresholds,
  verdictsOf,
  type Verdicts,
} from "../../workflows/hunt/config.js";
import { InvalidDecision, validateDecision } from "../../workflows/hunt/controller.js";
import type { DisconfirmationCritic } from "../../workflows/hunt/ports.js";
import { ScriptedDisconfirmationCritic } from "../../workflows/hunt/scripted.js";
import { evidenceStrength, isGap } from "../../workflows/hunt/strength.js";
import type { Digest, NullCheckInput, NullCheckResult } from "../../workflows/hunt/types.js";
import {
  controllerFor,
  evidenceOn,
  gapOn,
  huntSpecFor,
  newLedger,
  nullChecks,
  validateOn,
} from "../support/hunt.js";

const CONCLUDE = { action: "CONCLUDE" as const, rationale: "done" };

async function oneHypothesis() {
  const { ledger, hypothesisIds } = await newLedger();
  return { ledger, hypothesisId: hypothesisIds[0]! };
}

describe("argue the null", () => {
  it("blocks proven when the benign explanation stands, and files it as counter-evidence", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    const citations = [
      evidenceOn(ledger, hypothesisId, { source: "cloudtrail" }),
      evidenceOn(ledger, hypothesisId, { source: "duckdb" }),
    ];

    const result = await controllerFor(ledger, [validateOn(hypothesisId, citations)], {
      critic: new ScriptedDisconfirmationCritic(false),
    }).advanceIteration();

    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
    expect(result.note).toMatch(/benign explanation was not ruled out/);

    // The critic's argument is on the record like any other evidence, weakening
    // the hypothesis rather than deciding it.
    const record = nullChecks(ledger)[0]!;
    expect(record.source_system).toBe("critic");
    expect(record.salience).toBe("notable");
    expect(ledger.projection.links).toContainEqual({
      evidence_id: record.evidence_id,
      hypothesis_id: hypothesisId,
      relation: "weakens",
    });
    expect(ledger.projection.decisions).toHaveLength(1);
    expect(ledger.projection.decisions[0]!.decision.action).toBe("VALIDATE");
  });

  it("proves a hypothesis that survives it on two source systems", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    const citations = [
      evidenceOn(ledger, hypothesisId, { source: "cloudtrail" }),
      evidenceOn(ledger, hypothesisId, { source: "duckdb" }),
    ];

    const result = await controllerFor(ledger, [validateOn(hypothesisId, citations)], {
      critic: new ScriptedDisconfirmationCritic(true),
    }).advanceIteration();

    const hypothesis = ledger.projection.hypotheses.get(hypothesisId)!;
    expect(hypothesis.status).toBe("proven");
    expect(hypothesis.resolution_reason).toMatch(/survived the argue-the-null pass/);
    expect(result.note).toBe(`${hypothesisId} proven`);

    // The snapshot is what the numbers were at verdict time, so the verdict can
    // be re-read later without recomputing it against a moved ledger.
    expect(hypothesis.evidence_strength).toEqual({
      corroborating_sources: 2,
      contradicting_records: 0,
      open_gaps: 0,
      attacker_influenceable_only: false,
      survived_disconfirmation: true,
    });

    // A surviving null check is not itself support: it must not add a source.
    expect(nullChecks(ledger)).toHaveLength(1);
    expect(ledger.projection.links.filter((link) => link.relation === "supports")).toHaveLength(2);
  });

  it("hands the critic raw payloads rather than digest summaries", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    const citations = [evidenceOn(ledger, hypothesisId, { source: "cloudtrail" })];
    const critic = new ScriptedDisconfirmationCritic(true);

    await controllerFor(ledger, [validateOn(hypothesisId, citations)], { critic }).advanceIteration();

    const check = critic.checks[0] as NullCheckInput;
    expect(check.hypothesis_id).toBe(hypothesisId);
    expect(check.statement).toContain("a credential is used");
    expect(check.evidence[0]!.relation).toBe("supports");
    expect(check.evidence[0]!.record.payload).toEqual({ rows: 3, src_ip: "45.77.53.176" });
  });
});

describe("evidence_strength predicates", () => {
  it("refuses proven on a single corroborating source", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    const citations = [evidenceOn(ledger, hypothesisId, { source: "cloudtrail" })];

    const result = await controllerFor(ledger, [validateOn(hypothesisId, citations)], {
      critic: new ScriptedDisconfirmationCritic(true),
    }).advanceIteration();

    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
    expect(result.note).toMatch(/1 corroborating source system\(s\), 2 required/);
  });

  it("counts two records from the same source system as one", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    const citations = [
      evidenceOn(ledger, hypothesisId, { source: "cloudtrail" }),
      evidenceOn(ledger, hypothesisId, { source: "cloudtrail" }),
    ];

    await controllerFor(ledger, [validateOn(hypothesisId, citations)], {
      critic: new ScriptedDisconfirmationCritic(true),
    }).advanceIteration();

    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
    expect(evidenceStrength(ledger.projection, hypothesisId).corroborating_sources).toBe(1);
  });

  it("refuses proven when nothing supporting it was attested by the telemetry", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    const citations = [
      evidenceOn(ledger, hypothesisId, { source: "http", restsOn: [{ field: "filename", authored: "adversary" }] }),
      evidenceOn(ledger, hypothesisId, { source: "dns", restsOn: [{ field: "threat_label", authored: "third_party" }] }),
    ];

    const result = await controllerFor(ledger, [validateOn(hypothesisId, citations)], {
      critic: new ScriptedDisconfirmationCritic(true),
    }).advanceIteration();

    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
    expect(result.note).toMatch(/the telemetry attested/);
  });

  // Attacker-caused is not attacker-authored: the adversary chose the destination and
  // the interval, and the sensor counted the connections. A hunt whose every record is
  // flagged attacker-influenceable could not prove anything at any budget.
  it("allows proven when the support rests on what the sensor counted", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    const citations = [
      evidenceOn(ledger, hypothesisId, {
        source: "net_flow",
        attackerInfluenceable: true,
        restsOn: [{ field: "conn_count", authored: "sensor" }, { field: "dest_ip", authored: "adversary" }],
      }),
      evidenceOn(ledger, hypothesisId, {
        source: "endpoint",
        attackerInfluenceable: true,
        restsOn: [{ field: "parent_process", authored: "sensor" }],
      }),
    ];

    await controllerFor(ledger, [validateOn(hypothesisId, citations)], {
      critic: new ScriptedDisconfirmationCritic(true),
    }).advanceIteration();

    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("proven");
  });

  it("does not gate on stated_confidence", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    const citations = [evidenceOn(ledger, hypothesisId, { source: "cloudtrail" })];

    await controllerFor(ledger, [validateOn(hypothesisId, citations, { stated_confidence: 0.99 })], {
      critic: new ScriptedDisconfirmationCritic(true),
    }).advanceIteration();

    // Recorded for calibration, and inert: one source is still one source.
    expect(ledger.projection.decisions[0]!.decision.stated_confidence).toBe(0.99);
    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
  });

  it("attributes a gap to the hypothesis whose dispatch went unanswered", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    gapOn(ledger, hypothesisId);
    gapOn(ledger, null);

    expect(evidenceStrength(ledger.projection, hypothesisId).open_gaps).toBe(1);
    expect([...ledger.projection.evidence.values()].filter(isGap)).toHaveLength(2);
  });

  it("counts a repeated failure of one query as one gap, not three", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    for (let attempt = 0; attempt < 3; attempt += 1) gapOn(ledger, hypothesisId, "the same question");

    // Otherwise a flaky tool blinds a hypothesis it was only slow to answer.
    expect(evidenceStrength(ledger.projection, hypothesisId).open_gaps).toBe(1);
  });
});

describe("the argument has to be current", () => {
  // Argues one way, then the other, so a second verdict cannot coast on the first.
  class FlippingCritic implements DisconfirmationCritic {
    private calls = 0;
    async argueNull(): Promise<NullCheckResult> {
      const survives = (this.calls += 1) === 1;
      return {
        survives,
        strongest_benign_explanation: "a monitoring agent polling on a timer",
        rationale: survives ? "does not account for it" : "accounts for all of it",
        cost_usd: 0,
        model_id: "scripted",
        prompt_version: "scripted/v0",
      };
    }
  }

  it("refuses proven when the latest argument stands, whatever an earlier one said", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    const first = [evidenceOn(ledger, hypothesisId, { source: "cloudtrail" })];

    const controller = controllerFor(
      ledger,
      [
        validateOn(hypothesisId, first),
        // Cites what exists by then, which is more than the first pass argued over.
        (digest: Digest) => validateOn(hypothesisId, digest.recent_evidence.map((record) => record.evidence_id)),
      ],
      { critic: new FlippingCritic() as unknown as ScriptedDisconfirmationCritic },
    );

    // Survives, but on one source, so it stays up for a second look.
    await controller.advanceIteration();
    evidenceOn(ledger, hypothesisId, { source: "duckdb" });
    const second = await controller.advanceIteration();

    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
    expect(second.note).toMatch(/benign explanation was not ruled out/);
    expect(evidenceStrength(ledger.projection, hypothesisId).survived_disconfirmation).toBe(false);
  });

  it("records what the critic was shown, so the verdict can be re-read", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    const citations = [evidenceOn(ledger, hypothesisId, { source: "cloudtrail" })];

    await controllerFor(ledger, [validateOn(hypothesisId, citations)], {
      critic: new ScriptedDisconfirmationCritic(true),
    }).advanceIteration();

    expect(nullChecks(ledger)[0]!.payload["argued_evidence_ids"]).toEqual(citations);
  });
});

describe("gap lock", () => {
  it("closes a blinded hypothesis inconclusive, never disproven", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    const citations = [
      evidenceOn(ledger, hypothesisId, { source: "cloudtrail" }),
      evidenceOn(ledger, hypothesisId, { source: "duckdb" }),
    ];
    for (let gap = 0; gap < DEFAULT_VERDICTS.gap_lock_threshold; gap += 1) gapOn(ledger, hypothesisId);

    // Everything else clears, so only the gaps can be what stopped it. Three
    // distinct questions, because a retry of one is one blind spot.
    await controllerFor(ledger, [validateOn(hypothesisId, citations)], {
      critic: new ScriptedDisconfirmationCritic(true),
    }).advanceIteration();

    const hypothesis = ledger.projection.hypotheses.get(hypothesisId)!;
    expect(hypothesis.status).toBe("inconclusive");
    expect(hypothesis.resolution_reason).toMatch(/gap-locked/);
    expect(hypothesis.evidence_strength!.open_gaps).toBe(DEFAULT_VERDICTS.gap_lock_threshold);
  });

  it("locks whichever way the critic argued", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    const citations = [evidenceOn(ledger, hypothesisId, { source: "cloudtrail" })];
    for (let gap = 0; gap < DEFAULT_VERDICTS.gap_lock_threshold; gap += 1) gapOn(ledger, hypothesisId);

    await controllerFor(ledger, [validateOn(hypothesisId, citations)], {
      critic: new ScriptedDisconfirmationCritic(false),
    }).advanceIteration();

    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("inconclusive");
  });
});

describe("the verdict is the only writer", () => {
  it("leaves a proven hypothesis alone when the hunt ends", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    const citations = [
      evidenceOn(ledger, hypothesisId, { source: "cloudtrail" }),
      evidenceOn(ledger, hypothesisId, { source: "duckdb" }),
    ];

    const controller = controllerFor(ledger, [validateOn(hypothesisId, citations), CONCLUDE], {
      critic: new ScriptedDisconfirmationCritic(true),
    });
    await controller.advanceIteration();
    await controller.advanceIteration();

    expect(ledger.projection.hunt.outcome).toBe("completed");
    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("proven");
  });

  it("never reaches proven without a critic, and does not crash trying", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    const citations = [
      evidenceOn(ledger, hypothesisId, { source: "cloudtrail" }),
      evidenceOn(ledger, hypothesisId, { source: "duckdb" }),
    ];

    const result = await controllerFor(ledger, [validateOn(hypothesisId, citations)]).advanceIteration();

    expect(result.hunt_status).toBe("active");
    expect(result.note).toMatch(/no disconfirmation critic is configured/);
    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
    expect(nullChecks(ledger)).toHaveLength(0);
    expect(ledger.projection.decisions).toHaveLength(1);
  });

  it("keeps the hypothesis open when the critic itself fails", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    const citations = [
      evidenceOn(ledger, hypothesisId, { source: "cloudtrail" }),
      evidenceOn(ledger, hypothesisId, { source: "duckdb" }),
    ];
    const broken: DisconfirmationCritic = {
      async argueNull() {
        throw new Error("gateway unreachable");
      },
    };

    const result = await controllerFor(ledger, [validateOn(hypothesisId, citations)], {
      critic: broken as ScriptedDisconfirmationCritic,
    }).advanceIteration();

    // An argument that could not run is not an argument that was won.
    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
    expect(result.note).toMatch(/critic failed \(gateway unreachable\)/);
  });

  it("re-asks for a VALIDATE that names no hypothesis", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    const citations = [evidenceOn(ledger, hypothesisId, { source: "cloudtrail" })];
    const uncited = validateOn(hypothesisId, citations, { target_hypothesis_id: null });

    const result = await controllerFor(ledger, [uncited, CONCLUDE], {
      critic: new ScriptedDisconfirmationCritic(true),
    }).advanceIteration();

    expect(result.action).toBe("CONCLUDE");
    expect(ledger.projection.decisions[0]!.rejected_attempts![0]).toMatch(/must name the target_hypothesis_id/);
    expect(() => validateDecision(validateOn("h-nope", citations), ledger.projection)).toThrow(InvalidDecision);
  });
});

describe("what a verdict costs", () => {
  it("charges the critic call to the hunt", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    const citations = [evidenceOn(ledger, hypothesisId, { source: "cloudtrail" })];

    const result = await controllerFor(ledger, [validateOn(hypothesisId, citations)], {
      critic: new ScriptedDisconfirmationCritic(true, 0.02),
      costPerDecision: 0.05,
    }).advanceIteration();

    expect(ledger.projection.hunt.cost_usd).toBeCloseTo(0.07, 10);
    expect(result.cost_usd).toBeCloseTo(0.07, 10);
    // The decision record still carries what the decision cost, not the total.
    expect(ledger.projection.decisions[0]!.cost_usd).toBeCloseTo(0.05, 10);
  });
});

describe("verdict thresholds are config", () => {
  it("ships the documented defaults and honours an override", () => {
    expect(verdictsOf(huntSpecFor())).toEqual(DEFAULT_VERDICTS);
    expect(verdictsOf({ ...huntSpecFor(), thresholds: { min_corroborating_sources: 1 } })).toEqual({
      ...DEFAULT_VERDICTS,
      min_corroborating_sources: 1,
    });
  });

  it("refuses a threshold that would prove everything or lock everything", () => {
    expect(() => validateThresholds({ gap_lock_threshold: 0 })).toThrow(/must be a positive number/);
    expect(() => validateThresholds({ min_sources: 2 })).toThrow(/unknown thresholds key/);
  });

  it("proves on one source when the deployment says one source is enough", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    const citations = [evidenceOn(ledger, hypothesisId, { source: "cloudtrail" })];
    const verdicts: Verdicts = { min_corroborating_sources: 1, gap_lock_threshold: 3 };

    await controllerFor(ledger, [validateOn(hypothesisId, citations)], {
      critic: new ScriptedDisconfirmationCritic(true),
      verdicts,
    }).advanceIteration();

    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("proven");
  });
});

// Every optional field on the decision schema is nullable except one. A verb that
// carries no query — VALIDATE, ABANDON, CONCLUDE — emitted null for query_intent
// and was refused for a field it was right to leave empty, costing an attempt and
// a re-ask on the turn the hunt was trying to reach a verdict.
describe('a verb that carries no query', () => {
  it('declares query_intent nullable, like its siblings', async () => {
    const { readFileSync } = await import('node:fs')
    const { join } = await import('node:path')
    const arch = readFileSync(join(import.meta.dirname, '..', '..', 'arch', 'threathunt.yaml'), 'utf8')
    expect(arch).toContain('query_intent: { type: [string, "null"] }')
  })
})
