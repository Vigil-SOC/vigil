// The recall contract (#729): what a read of episodic memory returns, and the
// signature of the tool that performs one.
//
// One result shape, carried two ways. The run-start read journals it verbatim as
// the recall event payload; a mid-run recall_entity call carries the same object
// as the single row of a ToolResult. A second copy of the result would be a second
// contract, and the second one drifts.
//
// RecallUnavailable is not that second copy. It is the account of a read that did
// not happen, which is a different fact from a read that found nothing, and only
// the harness ever writes one -- Python answers reads and has no way to report
// that it was never asked.
//
// A mismatch between the halves fails as "no history", indistinguishable from an
// entity nobody has looked at. The Python half is core/memory/recall_contract.py
// and tests/unit/_ratchets/test_recall_contract_agrees.py fails when they
// disagree. It is a static check because neither side runs the other's code:
// Python answers the read and never journals it, the harness journals it and
// never runs the query.

import type { ToolResult } from "./tool.js";

export const RECALL_TOOL = "recall_entity";

// What the arch asks for; a ToolSpec binds it to the tool id via `provides`.
export const RECALL_CAPABILITY = "entity_recall";

// Keyed by the role names threathunt.yaml declares, not by a class called
// "worker" that appears in no file. Empties are decisions: the lead never
// queries, and narrative search -- the lead's alone -- ships no tool here.
export const RECALL_GRANTS = {
  lead: [],
  critic: [],
  threat_hunter: [RECALL_CAPABILITY],
  network_analyst: [RECALL_CAPABILITY],
  threat_intel: [RECALL_CAPABILITY],
} as const;

// Who concluded, as opposed to what the source is.
export const TRUST_LEVELS = ["analyst", "agent"] as const;
export type Trust = (typeof TRUST_LEVELS)[number];

// #728 owns this vocabulary; it is followed here, not restated.
export const SOURCE_TIERS = ["telemetry", "feed", "not_evidence"] as const;
export type SourceTier = (typeof SOURCE_TIERS)[number];

// Three-valued, because a flat corroborated list cannot express direction.
export const STANCES = ["supports", "weakens", "neither"] as const;
export type Stance = (typeof STANCES)[number];

// Keyed by kind and id, never run_id: a run-keyed schema cannot represent the
// Case-authored Verdicts the writer split requires. Nothing writes `analyst` yet.
export const INVESTIGATION_KINDS = ["hunt", "case", "analyst"] as const;
export type InvestigationKind = (typeof INVESTIGATION_KINDS)[number];

// handed_off is terminal for the hunt and not an ending of the claim, so it ranks
// up like a Gap while still carrying evidence, a window and sources.
// false_positive arrives only from a Case closure.
export const VERDICT_OUTCOMES = ["proven", "disproven", "inconclusive", "handed_off", "false_positive"] as const;
export type VerdictOutcome = (typeof VERDICT_OUTCOMES)[number];

// A retrospective sweep over old archives asserts its window rather than
// observing it, and ranking discounts the weaker one.
export const WINDOW_SOURCES = ["observed", "asserted"] as const;
export type WindowSource = (typeof WINDOW_SOURCES)[number];

// Each value traces to a line of the spec, because a disposition invented here is
// one #731 writes against and nothing reviews. Not `never_dispatched`: a
// hypothesis that reached inconclusive with nothing gathered *was* dispatched.
// Not `no_data_source` either -- a tool that could not answer is a Visibility
// Gap, which CONTEXT.md keeps distinct from a Declared Gap.
export const GAP_DISPOSITIONS = ["deprioritised", "no_evidence_gathered", "budget_exhausted"] as const;
export type GapDisposition = (typeof GAP_DISPOSITIONS)[number];

// The rule is `defang` then case-fold, in workflows/hunt/entities.ts. The
// exception is the half that breaks silently: an ARN's resource part and an AWS
// key id are case-significant, so folding them makes two principals one key and
// the join still returns rows, just the wrong ones.
export const KEY_CASE_SENSITIVE_TYPES = ["arn", "aws_key"] as const;

// Both ends inclusive. Separate from a conclusion date, because a sweep that
// concluded today about last March is not newly relevant.
export interface ActivityWindow {
  readonly first_seen: string;
  readonly last_seen: string;
}

// On every row, not the envelope: one read returns rows from many
// investigations. concluded_at is when the investigation concluded, not when the
// row was written -- the Distil polls, so an investigation that ended Monday can
// be written Wednesday carrying Monday's date, inside the freshness predicate and
// absent from the read that ran on Tuesday.
export interface RecalledProvenance {
  readonly investigation_kind: InvestigationKind;
  readonly investigation_id: string;
  readonly concluded_at: string;
}

// One row per entity, investigation and source, so growth tracks hunts and not
// telemetry volume.
export interface RecalledSighting extends RecalledProvenance {
  readonly entity_key: string;
  readonly source_system: string;
  readonly hit_count: number;
  readonly attacker_influenceable: boolean;
  readonly window: ActivityWindow;
}

// source_system is memory's own column, not a foreign key into either producer: a
// hunt-derived Verdict fills it from the Ledger, a Case-derived one from the
// data_source of its Findings. A not_evidence tier here is a defect rather than a
// weak row, and is representable so that it is visible.
export interface RecalledVerdictSource {
  readonly source_system: string;
  readonly stance: Stance;
  readonly source_tier: SourceTier;
}

export interface RecalledVerdict extends RecalledProvenance {
  // Stable, because the prose gets re-worded.
  readonly hypothesis_id: string;
  readonly statement: string;
  readonly outcome: VerdictOutcome;
  // Reaches no ranking function: confident-sounding model text must not move a
  // priority (ADR 0015). Carried for a human reading the run back.
  readonly rationale: string;
  // The entities the hypothesis named, typically one to three -- not the 38 to 76
  // its evidence touched, which stay reachable through Sightings (ADR 0016).
  readonly subject_entities: readonly string[];
  readonly attacker_influenceable_only: boolean;
  readonly trust: Trust;
  readonly window: ActivityWindow;
  readonly window_source: WindowSource;
  readonly sources: readonly RecalledVerdictSource[];
}

// No window, which is why a Gap is not a Verdict with an empty outcome.
export interface RecalledGap extends RecalledProvenance {
  readonly hypothesis_id: string;
  readonly statement: string;
  readonly disposition: GapDisposition;
  readonly reason: string;
  readonly subject_entities: readonly string[];
}

// Per reason, because the two mean different things to a caller: per_key_cap
// means one entity had more history than its share, overall means the read was
// broad. A total is the sum and is not restated.
export interface DroppedRows {
  readonly per_key_cap: number;
  readonly overall_cap: number;
}

export interface RecallDropped {
  readonly sightings: DroppedRows;
  readonly verdicts: DroppedRows;
  readonly gaps: DroppedRows;
}

// The parameters that chose this set, copied in rather than referenced: a RunSpec
// records the arch by name and not by version. Selection only -- the order the
// rows were *presented* in is a fold, so ranking may change without invalidating
// a historical Ledger.
export interface RecallRanking {
  readonly order: string;
  // Rows per kind, per key. Applied as a LATERAL and before the overall cap.
  readonly per_key_cap: number;
  // Rows across all kinds and keys.
  readonly overall_cap: number;
}

// Named as a value too, so recallOf can check for them at runtime, which is where
// the drift arrives.
export const RECALL_RESULT_KEYS = [
  "keys",
  "as_of",
  "sightings",
  "verdicts",
  "gaps",
  "dropped",
  "ranking",
] as const;

// No field is optional and no list nullable: an empty list means
// known-to-be-none, so an entity with no history is empty lists and zeros rather
// than an error.
export interface RecallResult {
  // Normalised, and as queried rather than as asked for: only Python writes an
  // Entity Key, so the extractor's output travels as candidate data.
  readonly keys: readonly string[];
  // The freshness filter that ran, and not a substitute for journaling the rows.
  readonly as_of: string;
  readonly sightings: readonly RecalledSighting[];
  readonly verdicts: readonly RecalledVerdict[];
  readonly gaps: readonly RecalledGap[];
  readonly dropped: RecallDropped;
  readonly ranking: RecallRanking;
}

// A read that did not happen, journaled in place of a result. Not an empty
// RecallResult: empty lists mean known-to-be-none, so recording an outage as one
// would say these entities have no history -- true of every entity while memory is
// down, so nothing would look wrong.
//
// The keys are still here because they are what the run asked about, and a reader
// working out what a run was denied needs them.
export interface RecallUnavailable {
  readonly keys: readonly string[];
  readonly as_of: string;
  // Why the read did not happen, in the words of whatever refused it.
  readonly unavailable: string;
}

// The event payload is the result, or the account of why there is none. An alias
// for the first, because a copied interface would say only that the two happen not
// to have diverged yet.
export type RecallPayload = RecallResult | RecallUnavailable;

// Both key arguments are optional and at least one is required. The singular
// exists for the caller that sends one string *instead of* the list, so requiring
// the list would make that call invalid; the schema cannot express "one of", so
// the invariant is data and is checked at the call.
export const RECALL_KEY_ARGS = ["entity_keys", "entity_key"] as const;

export interface RecallArgs {
  readonly entity_keys?: readonly string[];
  readonly entity_key?: string;
  // Every read takes one, rather than an as-of read being a tool of its own.
  readonly as_of?: string;
  // For the read log, which is #732's. Defaulted rather than required: an
  // unattributed read is still worth logging.
  readonly caller_kind?: string;
  readonly caller_id?: string;
}

// Two points this contract does not settle.
//
// `dropped` diverges from #732's grooming note, which reads three integers where
// #729 asks for the count *and why*.
//
// Per-entity counts of open questions are the one ranking input the spec names
// that no field here carries. Nearly derivable -- Gaps plus handed_off Verdicts,
// grouped by subject_entities -- except the per-key cap truncates precisely the
// entities the count exists to surface, and dropped is aggregated across the read
// rather than per key. Both fixes add a key; the ranking is the harness's, so the
// choice is the harness's.

// Known-to-be-none rather than an error: an entity nobody has looked at is empty
// lists and zeros, and every caller reads the same shape.
//
// The ranking is the read's own account of what chose the set. Nothing chose
// anything here, so the caps are zero and the order is named for that.
export const emptyRecall = (keys: readonly string[], asOf: string): RecallResult => {
  const none = { per_key_cap: 0, overall_cap: 0 };
  return {
    keys,
    as_of: asOf,
    sightings: [],
    verdicts: [],
    gaps: [],
    dropped: { sightings: none, verdicts: none, gaps: none },
    ranking: { order: "none", per_key_cap: 0, overall_cap: 0 },
  };
};

// The one renderer, used by the run that recalls and by the rebuild that replays
// it. Two renderers would agree until one of them was edited.
//
// A pure function of the journaled rows: `ranking` is read by nothing here. The
// parameters chose the set when the read ran, and the set is what was journaled --
// a renderer that re-capped or re-sorted under today's parameters would present a
// decision with something other than what it was made against (ADR 0015).

const windowOf = ({ first_seen, last_seen }: { first_seen: string; last_seen: string }): string =>
  first_seen === last_seen ? first_seen : `${first_seen} to ${last_seen}`;

// Kind and id rather than run id: one read returns rows from many investigations,
// and a Case is not a hunt.
const provenanceOf = (row: RecalledProvenance): string =>
  `${row.investigation_kind} ${row.investigation_id}, concluded ${row.concluded_at}`;

const verdictNote = (verdict: RecalledVerdict): string => {
  const held = [
    `${verdict.outcome}: ${verdict.statement}`,
    `subjects ${verdict.subject_entities.join(", ")}`,
    `activity ${windowOf(verdict.window)}${verdict.window_source === "asserted" ? " (asserted)" : ""}`,
    `${verdict.trust}, from ${provenanceOf(verdict)}`,
  ];
  // The one thing a reader must not miss: a conclusion resting only on fields an
  // adversary could have written is not a conclusion to lean on.
  if (verdict.attacker_influenceable_only) held.push("rests only on attacker-influenceable evidence");
  if (verdict.rationale !== "") held.push(`because ${verdict.rationale}`);
  return held.join(" — ");
};

const gapNote = (gap: RecalledGap): string =>
  [
    `unanswered: ${gap.statement}`,
    `subjects ${gap.subject_entities.join(", ")}`,
    `${gap.disposition}: ${gap.reason}`,
    `from ${provenanceOf(gap)}`,
  ].join(" — ");

const sightingNote = (sighting: RecalledSighting): string => {
  const held = [
    `${sighting.entity_key} seen ${sighting.hit_count}x on ${sighting.source_system}`,
    windowOf(sighting.window),
    `from ${provenanceOf(sighting)}`,
  ];
  if (sighting.attacker_influenceable) held.push("attacker-influenceable");
  return held.join(" — ");
};

// Per kind and per reason, because the two reasons mean different things to a
// reader: a per-key cap says one entity had more history than its share, and an
// overall cap says the read was simply broad. Summing them keeps the number and
// discards the half a reader would act on.
const DROPPED_KINDS = ["sightings", "verdicts", "gaps"] as const;

const droppedNote = (result: RecallResult): string | null => {
  const held: string[] = [];
  for (const kind of DROPPED_KINDS) {
    const { per_key_cap, overall_cap } = result.dropped[kind];
    if (per_key_cap > 0) held.push(`${per_key_cap} ${kind} past one entity's share`);
    if (overall_cap > 0) held.push(`${overall_cap} ${kind} past the read's own limit`);
  }
  return held.length === 0 ? null : `Some history was not carried: ${held.join(", ")}.`;
};

// What an earlier investigation concluded before where an entity was seen: a
// settled claim bears on the run's own question and a sighting is a lead. A fixed
// presentation fold, not a parameter -- see the note at the top of this file.
export function recalledNotes(result: RecallResult): readonly string[] {
  const notes = [
    ...result.verdicts.map(verdictNote),
    ...result.gaps.map(gapNote),
    ...result.sightings.map(sightingNote),
  ];
  if (notes.length === 0) return [];
  const dropped = droppedNote(result);
  return dropped === null ? notes : [...notes, dropped];
}

// Only what the two functions below read off an event: the kind that selects it
// and the payload they render. Narrower than an AgentEvent on purpose -- this
// contract is consumed by the harness and by Python, and neither needs the
// envelope to render a row.
export interface LedgerRecord {
  kind: string;
  payload: unknown;
}

// Which of the two a journaled payload is. Keyed on the field only the account of
// an outage carries, so a result is never mistaken for one.
export function isRecalled(payload: RecallPayload): payload is RecallResult {
  return !("unavailable" in payload);
}

// Whether the run has read memory at all. Its own function because the three
// answers a reader needs -- rows, an outage, nothing yet -- are two questions: a
// read that found nothing and a read that could not be served both render to no
// notes, and only the last of the three means ask memory.
//
// A run that has read does not read again: a read that succeeded on turn four
// would move a prefix the run had already decided against (ADR 0009).
export function hasRecall(log: readonly LedgerRecord[]): boolean {
  return log.some((one) => one.kind === "recall");
}

// The rows a run opened on, off its own ledger. The first recall event, because a
// run recalls once at start; a second one is a later read and not what the opening
// prefix carried.
//
// Null for an outage and null for a payload this cannot read, both of which are
// journaled reads with no rows to present -- hasRecall is what tells those from a
// run that has not asked.
export function recalledRowsOf(log: readonly LedgerRecord[]): RecallResult | null {
  const event = log.find((one) => one.kind === "recall");
  if (event === undefined) return null;
  const payload = event.payload;
  if (typeof payload !== "object" || payload === null) return null;
  // Validated rather than cast: a drifted payload cast to a RecallResult renders
  // as an entity nobody has looked at -- see RecallUnavailable for why that
  // particular wrong answer is the one that hides.
  return RECALL_RESULT_KEYS.every((key) => key in payload) ? (payload as RecallResult) : null;
}

// The journaled payload whole, outage included, for a reader that presents the read
// rather than rendering its rows. recalledRowsOf answers null for an outage and null
// for a run that never asked, and those are not the same fact: one says memory was
// down, the other that nothing was ever looked up. A report that printed them alike
// would be the wrong answer nobody spots -- see RecallUnavailable.
export function recalledPayloadOf(log: readonly LedgerRecord[]): RecallPayload | null {
  const event = log.find((one) => one.kind === "recall");
  if (event === undefined) return null;
  const payload = event.payload;
  if (typeof payload !== "object" || payload === null) return null;
  if ("unavailable" in payload) return payload as RecallUnavailable;
  return recalledRowsOf(log);
}

// The rebuild: the notes those rows render to. Pure over the log and reaching no
// Memory -- a replay that re-reads memory reads a neighbourhood that has moved
// since the run, which looks like a passing test until it looks like a wrong
// answer.
export function recalledNotesOf(log: readonly LedgerRecord[]): readonly string[] {
  const held = recalledRowsOf(log);
  return held === null ? [] : recalledNotes(held);
}

// One row holding the whole mapping. It validates rather than casting, because a
// cast would let a drifted payload through as a RecallResult of undefined fields
// -- see RecallUnavailable for why that renders as the wrong answer nobody spots.
export const recallOf = (result: ToolResult): RecallResult | null => {
  if (!result.ok || result.rowCount !== 1 || result.rows.length !== 1) return null;
  const row = result.rows[0];
  if (typeof row !== "object" || row === null) return null;
  return RECALL_RESULT_KEYS.every((key) => key in row) ? (row as RecallResult) : null;
};
