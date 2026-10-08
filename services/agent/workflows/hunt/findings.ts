import type { Projection } from "./ledger.js";

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

// The alerts the hunt's evidence cites: rows carrying a finding_id, as findings_search
// and get_finding return them. One that only turned up in a search result and never
// became evidence is not here, so it is not linked to the case.
export function citedFindings(projection: Projection): string[] {
  const ids = new Set<string>();
  for (const record of projection.evidence.values()) collect(record.payload, 0, ids);
  return [...ids];
}
