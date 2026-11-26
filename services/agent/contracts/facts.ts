// The entity-facts read a run opens on (#4742): what Entra, Okta and Falcon said
// about its trigger findings' users and hosts. Journaled verbatim as the run's
// facts event; the prefix block and a replay both render from that event, never
// from the table. The Python half is read_keys in core/integrations/entity_facts.py,
// answered as FactRead.journaled().
import type { ToolResult } from "./tool.js";
import type { LedgerRecord } from "./memory.js";

export const FACTS_TOOL = "read_entity_facts";

// One row as read: the Entity Key, the entity as the key spells it, the source's
// label, when the source answered (UTC), and the projected fields. A "Declared" row
// (#5211) is an item of the applied site revision instead: fetched_at is when that
// revision was applied, and its fields carry kind, name, file, line and revision.
export interface FactRow {
  readonly entity: string;
  readonly value: string;
  readonly source: string;
  readonly fetched_at: string;
  readonly fields: Readonly<Record<string, unknown>>;
}

// A read that happened. A source that could not answer is named with its reason;
// the rest of the read stands.
export interface EntityFacts {
  // The keys looked up: those of a kind some source answers, up to the read's limit.
  readonly keys: readonly string[];
  readonly case_id: string | null;
  readonly facts: readonly FactRow[];
  readonly sources_unavailable: Readonly<Record<string, string>>;
  readonly dropped_keys: number;
}

// A read that did not happen at all -- the endpoint failed or refused, or the run
// may not read its case. Not an empty EntityFacts, for the reason RecallUnavailable
// is not an empty recall: no facts read would say no source had anything.
export interface FactsUnavailable {
  readonly keys: readonly string[];
  readonly case_id: string | null;
  readonly unavailable: string;
}

export type FactsPayload = EntityFacts | FactsUnavailable;

// The source of a row from the operator's site files rather than from a source.
export const DECLARED = "Declared";

const RESULT_KEYS = ["keys", "facts", "sources_unavailable", "dropped_keys"] as const;

export function isFactsRead(payload: FactsPayload): payload is EntityFacts {
  return !("unavailable" in payload);
}

// The tool's one row, validated rather than cast.
export function factsOf(result: ToolResult, caseId: string | null): EntityFacts | null {
  if (!result.ok || result.rows.length !== 1) return null;
  const row = result.rows[0] as Record<string, unknown> | null;
  if (typeof row !== "object" || row === null || !RESULT_KEYS.every((key) => key in row)) return null;
  if (!Array.isArray(row["keys"]) || !Array.isArray(row["facts"])) return null;
  return {
    keys: row["keys"] as string[],
    case_id: caseId,
    facts: row["facts"] as FactRow[],
    sources_unavailable: (row["sources_unavailable"] ?? {}) as Record<string, string>,
    dropped_keys: Number(row["dropped_keys"]) || 0,
  };
}

// The first facts event: a run reads once, at start, and carries it for its life.
export function factsPayloadOf(log: readonly LedgerRecord[]): FactsPayload | null {
  const payload = log.find((one) => one.kind === "facts")?.payload;
  return typeof payload === "object" && payload !== null ? (payload as FactsPayload) : null;
}

export function hasFacts(log: readonly LedgerRecord[]): boolean {
  return log.some((one) => one.kind === "facts");
}

// Sorted: the journal is jsonb, which reorders keys, and a resume or replay must
// render the bytes the first turn did.
const sorted = (held: Readonly<Record<string, unknown>>): [string, unknown][] => Object.entries(held).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));

const shown = (value: unknown): string => (Array.isArray(value) ? value.map(String).join(", ") : String(value));

// The lines the prefix block holds, one renderer for the run and its replay. Empty
// when nothing was asked: a run whose findings named no user or host sees no block.
export function factLines(payload: FactsPayload): readonly string[] {
  if (!isFactsRead(payload)) return [`Entity facts could not be read for this run: ${payload.unavailable}`];
  if (payload.keys.length === 0) return [];
  const lines = payload.facts.map(
    (fact) =>
      `${fact.source} on ${fact.entity} (${fact.source === DECLARED ? "applied" : "fetched"} ${fact.fetched_at}): ` +
      sorted(fact.fields)
        .map(([name, value]) => `${name}: ${shown(value)}`)
        .join("; "),
  );
  for (const [source, reason] of sorted(payload.sources_unavailable)) lines.push(`${source}: unavailable (${String(reason)})`);
  if (payload.dropped_keys > 0) lines.push(`${payload.dropped_keys} more users or hosts were not looked up.`);
  return lines.length === 0 ? ["No source had anything on these users or hosts."] : lines;
}
