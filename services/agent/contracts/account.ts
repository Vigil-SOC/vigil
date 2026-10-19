// What a run concluded, in the one shape every loop fills and every screen reads.
// Hunt, investigate and root cause each write their result their own way; an adapter
// per loop turns that into an account, and the backend merges a case's accounts on
// read. Mirrored in core/storage/schemas/case_account.py, and the two are held to the
// same JSON by the fixtures under tests/fixtures/account.
//
// A loop fills only the sections it produces. An absent section means "this loop does
// not produce it", never "it found none": root cause fills chain and leaves
// explanations out, and an empty list is a loop that looked and found nothing. For the
// same reason nothing in an account is null. A value the loop does not have is absent.
//
// Every item names the run it came from, because a case's account is its runs' items
// side by side, and a reader has to be able to tell whose claim each one is.

import type { RunKind } from "./events.js";

// Changing a field's meaning bumps this. Adding an optional field does not.
export const ACCOUNT_SCHEMA_VERSION = 1;

export interface AccountRun {
  run_id: string;
  run_kind: RunKind;
}

// The investigate conclusion's own vocabulary, so the loop that already answers in it
// needs no mapping. needs_review is a run that would not stand behind a dismissal.
export const DISPOSITIONS = ["malicious", "suspicious", "benign", "false_positive", "inconclusive", "needs_review"] as const;
export type Disposition = (typeof DISPOSITIONS)[number];

export const CONFIDENCES = ["low", "medium", "high"] as const;
export type Confidence = (typeof CONFIDENCES)[number];

export interface Verdict {
  run_id: string;
  disposition: Disposition;
  // Absent when the loop states none. A hunt gates its verdict on evidence strength
  // and never asks the model how sure it is, so an adapter must not invent one.
  confidence?: Confidence;
  headline: string;
  // Why the run lands where it does, in a line: the deciding explanation's resolution,
  // or the root cause's count of confirmed upstream events.
  basis?: string;
}

export interface Narrative {
  run_id: string;
  summary: string;
  // Markdown. Absent on a loop whose summary is the whole account.
  what_happened?: string;
  // "vigil" when the model wrote no account and this one was laid out from the
  // ledger, with why the model's did not land. Absent on an account the model wrote.
  written_by?: "vigil";
  missing_because?: string;
  model_id?: string;
}

// The two sides a case is argued between. The merge pairs explanations across runs by
// role, since two runs never word the same claim the same way.
export const EXPLANATION_ROLES = ["adversary", "benign"] as const;
export type ExplanationRole = (typeof EXPLANATION_ROLES)[number];

// How a claim stands when the run ended. proven, ruled_out, inconclusive and set_aside
// are a run's ruling; supported and weakened are read off the evidence by a loop that
// keeps a record but rules on nothing; open is a claim nobody settled either way.
export const STANDINGS = ["proven", "supported", "open", "inconclusive", "weakened", "ruled_out", "set_aside"] as const;
export type Standing = (typeof STANDINGS)[number];

export interface Explanation {
  id: string;
  run_id: string;
  claim: string;
  role: ExplanationRole;
  standing: Standing;
  // Why it stands where it does, in the run's words.
  reason?: string;
  techniques?: string[];
  // What the claim is about, as its author declared it. Never extracted from the
  // sentence: a claim mentions far more entities than it is about.
  subjects?: EntityKey[];
}

export const STANCES = ["supports", "weakens", "neither"] as const;
export type Stance = (typeof STANCES)[number];

// What one record bears on: an explanation or a chain event of the same run, by id.
export interface Bearing {
  on: string;
  stance: Stance;
}

// A value of a record checked against the results it cites, by code rather than by
// the model that wrote both. The hunt's record checks are this shape, and
// plugins/hunt/checks.ts should import it from here once they land.
export const CHECK_STATUSES = ["match", "differs", "absent"] as const;
export type CheckStatus = (typeof CHECK_STATUSES)[number];

export interface ValueCheck {
  field: string;
  claimed: unknown;
  status: CheckStatus;
  // Where it was found: the cited call, the result's own field name and its value.
  ref?: string;
  found_field?: string;
  found?: unknown;
  // The claimed number is the found one rounded or cut to the places the record wrote.
  rounded?: true;
}

// How directly a record shows what it is cited for, in the root-cause graph's terms.
export const STRENGTHS = ["direct", "corroborating", "circumstantial"] as const;
export type Strength = (typeof STRENGTHS)[number];

export const SALIENCES = ["routine", "notable", "anomalous"] as const;
export type Salience = (typeof SALIENCES)[number];

export interface EvidenceItem {
  id: string;
  run_id: string;
  claim: string;
  why?: string;
  // The system the record was read from, such as zeek:conn or win_events.
  source?: string;
  // The turn or iteration the run recorded it in.
  turn?: number;
  salience?: Salience;
  strength?: Strength;
  // Set on a search that came back empty on purpose: the record is that something is
  // not there. A query that could not run is a gap, not this.
  absence?: true;
  // Whether the values the claim rests on are ones the sensor wrote. Absent when the
  // loop does not judge it, which is not the same as false.
  attested?: boolean;
  // The search a reader opens to check the record, and every search it cites, the
  // first one first. Refs are the run's own (r7, tc_25).
  source_ref?: string;
  source_refs?: string[];
  checks?: ValueCheck[];
  bears_on?: Bearing[];
  techniques?: string[];
  entities?: EntityKey[];
}

// A chain event's standing, and the standing of what ties it to the next one. The
// graph loop says confirmed and suspected; the step loop says proven and unproven, and
// its proven is this confirmed.
export const CHAIN_STATUSES = ["confirmed", "suspected", "unproven", "rejected"] as const;
export type ChainStatus = (typeof CHAIN_STATUSES)[number];

// What makes one event the cause of the next: the value both carry, and how well the
// run established it.
export interface ChainTie {
  id?: string;
  text?: string;
  status: ChainStatus;
  confidence?: number;
}

export interface ChainEvent {
  id: string;
  run_id: string;
  text: string;
  at?: string;
  source?: string;
  status: ChainStatus;
  // 0 to 1. Absent on a loop that proves or fails to and scores nothing.
  confidence?: number;
  // The event this one led to, toward the symptom, and what ties the two. Absent on
  // the symptom, and on an event the run never tied to anything.
  causes?: string;
  tie?: ChainTie;
  // Events that together with this one caused the same next event.
  together_with?: string[];
  // Set on an event the run names as where it began, with how well that claim holds
  // and why nothing earlier explains it. A run that traced several threads may name
  // an origin on more than one.
  origin?: ChainStatus;
  why_origin?: string;
  ran_as?: string;
  operated_by?: string;
  entities?: EntityKey[];
}

// How it started: every event the run placed on a causal path. The path from an
// origin to the symptom is read by following causes; a run that traced more than one
// thread holds several, each ending at an event with no causes.
export interface Chain {
  run_id: string;
  events: ChainEvent[];
  // 0 to 1, how well the whole path holds.
  score?: number;
  alternatives?: ChainPath[];
}

// Another path the run kept, symptom first, by event id.
export interface ChainPath {
  path: string[];
  score?: number;
}

export interface TimelineEntry {
  run_id: string;
  text: string;
  // ISO 8601, to the precision the run knows. Absent when it knows none: an entry
  // with no time still belongs on the timeline, sorted by the ones around it.
  at?: string;
  // The kill-chain stage, in the run's words, such as INITIAL ACCESS.
  stage?: string;
  techniques?: string[];
  // The chain event or evidence item this entry is, when it is one.
  ref?: string;
  source?: string;
  status?: ChainStatus;
  confidence?: number;
  on_chain?: boolean;
  // Activity the run found to be someone's routine work, kept because the attack
  // passed through it.
  legitimate?: true;
  ran_as?: string;
  operated_by?: string;
}

export const ACCOUNT_ROLES = ["attacker", "victim", "legitimate"] as const;
export type AccountRole = (typeof ACCOUNT_ROLES)[number];

// Who the actions ran as and who operated them. A victim account is one the attacker
// used; the person behind it is not the attacker, and the account says so.
export interface Account {
  run_id: string;
  name: string;
  role: AccountRole;
  note?: string;
  // What the logs show of it when they do not name it: a domain, an address.
  seen_as?: string[];
  entities?: EntityKey[];
}

// Closed, so a typo cannot invent a type and every loop's entities land in one
// namespace. Owned here: what the extractor emits, what a hunt's subject may name and
// what a recall key may hold are this list, and Python's copy is ratcheted to it.
export const ENTITY_TYPES = ["ip", "domain", "host", "url", "email", "hash", "arn", "aws_key", "user", "process"] as const;
export type EntityType = (typeof ENTITY_TYPES)[number];

// type:value, the form a hunt already writes an entity in, normalized the extractor's
// way: host:acct-ws-007.
export type EntityKey = `${EntityType}:${string}`;

export interface EntityItem {
  key: EntityKey;
  run_id: string;
  // The run names it an indicator of compromise: something to block or hunt for,
  // not only something the attack touched.
  indicator?: true;
}

export interface NextStep {
  run_id: string;
  text: string;
}

// visibility is a query that could not run or answer; open_question is something the
// run asked and could not settle; disputed is a tie the run itself argued both ways.
export const GAP_KINDS = ["visibility", "open_question", "disputed"] as const;
export type GapKind = (typeof GAP_KINDS)[number];

export interface Gap {
  run_id: string;
  text: string;
  kind?: GapKind;
  // The explanation or chain event it leaves open, by id.
  on?: string;
}

// A claim the run moved to incident response. The case it names must exist.
export interface Escalation {
  run_id: string;
  case_id: string;
  rationale: string;
  explanation_id?: string;
  at?: string;
}

// The run's moves, in order. The one section whose vocabulary is the loop's own: a
// hunt INVESTIGATEs and PIVOTs, root cause EXPANDs and TESTs.
export interface Step {
  run_id: string;
  n: number;
  kind: string;
  text: string;
  at?: string;
  cost_usd?: number;
  duration_ms?: number;
  refs?: string[];
}

export interface CaseAccount {
  schema_version: number;
  // The runs this account is built from: one for a run's own, every run on the case
  // for a case's. A loop's absent sections are read against what it is.
  runs: AccountRun[];
  verdict?: Verdict;
  narrative?: Narrative;
  explanations?: Explanation[];
  evidence?: EvidenceItem[];
  chain?: Chain;
  timeline?: TimelineEntry[];
  accounts?: Account[];
  entities?: EntityItem[];
  next_steps?: NextStep[];
  gaps?: Gap[];
  escalations?: Escalation[];
  steps?: Step[];
}
