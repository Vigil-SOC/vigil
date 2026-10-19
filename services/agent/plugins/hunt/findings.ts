import type { Projection } from "./ledger.js";
import { BASE_RATE_PROVENANCE } from "./opening.js";
import type { EvidenceRecord } from "./types.js";

// Deep enough for a salvaged record ({ gathered: [{ rows: [{ finding_id }] }] }) and
// no deeper: the payload is attacker-reachable, so a walk over it stays bounded.
const MAX_DEPTH = 6;

function collect(value: unknown, depth: number, into: Set<string>): void {
  if (depth > MAX_DEPTH || typeof value !== "object" || value === null) return;
  if (Array.isArray(value)) {
    for (const item of value) collect(item, depth + 1, into);
    return;
  }
  for (const [key, inner] of Object.entries(value)) {
    if (key === "finding_id" && typeof inner === "string" && inner.trim() !== "") into.add(inner.trim());
    else collect(inner, depth + 1, into);
  }
}

// The alerts one record's rows carry a finding_id for, as findings_search and
// get_finding return them.
export function findingsCitedBy(record: EvidenceRecord): string[] {
  const ids = new Set<string>();
  collect(record.payload, 0, ids);
  return [...ids];
}

// The alerts a hunt links onto its case: those cited by evidence the hunt ruled
// supports or weakens on one of the case's own explanations (#2289). An alert the hunt
// looked at and found bearing on nothing, or only on the benign explanation, stays on
// the run's record: merged into the case, its hosts and users would describe noise.
// One that only turned up in a search result and never became evidence is not here either.
export function citedFindings(projection: Projection): string[] {
  const ids = new Set<string>();
  for (const link of projection.links) {
    if (link.relation === "neither") continue;
    if (projection.hypotheses.get(link.hypothesis_id)?.provenance === BASE_RATE_PROVENANCE) continue;
    const record = projection.evidence.get(link.evidence_id);
    if (record !== undefined) collect(record.payload, 0, ids);
  }
  return [...ids];
}
