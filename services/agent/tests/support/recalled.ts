import { emptyRecall, type RecallResult } from "../../contracts/memory.js";
import type { Memory } from "../../core/seams.js";

// One recalled set, shared by everything that needs one. Four copies of it drifted
// apart the moment one of them was edited.
//
// Sanitised by construction -- documentation addresses, an example.com principal,
// pseudonymous ids -- because a recorded run carrying it is committed, and the
// fixture sweep holds it to the same allowlist as the ten historical ledgers.
export const RECALL_KEYS = ["ip:192.0.2.10", "user:example\\user0001"] as const;

export const recalledFixture = (over: Partial<RecallResult> = {}): RecallResult => ({
  ...emptyRecall(RECALL_KEYS, "2026-03-07T00:00:00.000Z"),
  verdicts: [
    {
      hypothesis_id: "hyp-0001",
      statement: "192.0.2.10 is a scheduled backup target",
      outcome: "false_positive",
      rationale: "the platform team confirmed the overnight schedule",
      subject_entities: ["ip:192.0.2.10"],
      attacker_influenceable_only: false,
      trust: "analyst",
      window: { first_seen: "2026-02-01", last_seen: "2026-03-01" },
      window_source: "observed",
      sources: [{ source_system: "zeek", stance: "weakens", source_tier: "telemetry" }],
      investigation_kind: "case",
      investigation_id: "case-0007",
      concluded_at: "2026-03-02",
    },
  ],
  gaps: [
    {
      hypothesis_id: "hyp-0002",
      statement: "the workstation's process telemetry would settle the overnight traffic",
      disposition: "no_evidence_gathered",
      reason: "the endpoint tool answered nothing on either attempt",
      subject_entities: ["host:host0001-L"],
      investigation_kind: "hunt",
      investigation_id: "hunt-0002",
      concluded_at: "2026-03-04",
    },
  ],
  sightings: [
    {
      entity_key: "user:example\\user0001",
      source_system: "okta",
      hit_count: 3,
      attacker_influenceable: false,
      window: { first_seen: "2026-03-01", last_seen: "2026-03-04" },
      investigation_kind: "hunt",
      investigation_id: "hunt-0002",
      concluded_at: "2026-03-04",
    },
  ],
  dropped: {
    sightings: { per_key_cap: 4, overall_cap: 0 },
    verdicts: { per_key_cap: 0, overall_cap: 0 },
    gaps: { per_key_cap: 0, overall_cap: 0 },
  },
  ranking: { order: "recency", per_key_cap: 20, overall_cap: 200 },
  ...over,
});

export interface Counting extends Memory {
  // Every keyed read, in order, so a test can assert both how many happened and
  // what they asked about.
  reads: () => readonly (readonly string[])[];
  // The freshness boundary the last read was asked for.
  asOf: () => string | null;
}

// Counts, because the rule is that a run reads once and a rebuild never does.
export function countingMemory(result: RecallResult = recalledFixture()): Counting {
  const reads: string[][] = [];
  let asOf: string | null = null;
  return {
    reads: () => reads,
    asOf: () => asOf,
    recall: async () => [],
    entities: async ({ keys, asOf: stamp }) => {
      reads.push([...keys]);
      asOf = stamp;
      return result;
    },
    remember: async () => {},
  };
}
