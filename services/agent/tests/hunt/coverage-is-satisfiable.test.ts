import { describe, expect, it } from "vitest";
import { Ajv } from "ajv";
import { join } from "node:path";
import { archFor } from "../../arch/registry.js";
import { buildSpec } from "../../core/spec.js";
import { validateDecision } from "../../workflows/hunt/controller.js";
import { unclassified } from "../../workflows/hunt/strength.js";
import type { Decision } from "../../workflows/hunt/types.js";
import { bareEvidence, newLedger } from "../support/hunt.js";

const FIXTURES = join(import.meta.dirname, "..", "fixtures");

function leadSchema(): Record<string, unknown> {
  const entry = archFor("hunt");
  const spec = buildSpec(
    { arch: entry.arch, playbook: join(FIXTURES, "hunt.playbook.yaml"), config: join(FIXTURES, "hunt.config.yaml") },
    entry.actions,
  );
  return spec.roles.lead!.output_schema as Record<string, unknown>;
}

// validateCoverage refuses any decision that leaves an observation unruled against an
// active hypothesis, and it reads decision.evidence_relations to see the rulings. The
// shipped schema set additionalProperties: false and declared no such property, so the
// field was unemittable: from the moment the first worker answered, every decision was
// refused three times for something the lead had no way to say, and the hunt stalled at
// iteration one with two records and no verdict. A guard is only a guard if the answer
// it demands can be given.
describe("the coverage guard asks for something the lead can emit", () => {
  it("lets the declared schema carry the rulings the controller demands", () => {
    const validate = new Ajv({ allErrors: true, strict: false }).compile(leadSchema());
    const decision = {
      action: "INVESTIGATE",
      rationale: "rule what came back, then ask again",
      evidence_citations: [],
      evidence_relations: [{ evidence_id: "ev-1", hypothesis_id: "h-1", relation: "neither" }],
    };

    expect(validate(decision)).toBe(true);
  });

  it("names the three rulings the link relation actually has", () => {
    const relations = leadSchema()["properties"] as Record<string, Record<string, never>>;
    const item = (relations["evidence_relations"] as unknown as { items: { properties: { relation: { enum: string[] } } } }).items;

    expect(item.properties.relation.enum).toEqual(["supports", "weakens", "neither"]);
  });

  // The other half: ruling every pair the guard names has to actually satisfy it, or the
  // lead can emit the field and still be refused forever.
  it("accepts a decision that rules every pair the guard named", async () => {
    const { ledger } = await newLedger();
    // Unlinked on purpose: what the guard names is exactly the pairs nothing rules yet.
    bareEvidence(ledger);

    const missing = unclassified(ledger.projection);
    expect(missing.length).toBeGreaterThan(0);

    const decision = {
      action: "INVESTIGATE",
      rationale: "ruled",
      evidence_citations: [],
      query_intent: "next",
      evidence_relations: missing.map((pair) => ({ ...pair, relation: "neither" as const })),
    } as Decision;

    expect(() => validateDecision(decision, ledger.projection)).not.toThrow();
  });
});
