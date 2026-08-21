import { DEFAULT_PARK_MS, type BudgetLimits } from "../../contracts/budget.js";
// Type-only, so the cycle with the spec loader is erased at compile time.
import type { HuntSpec } from "./config.js";



export type DecisionAction =
  | "INVESTIGATE"
  | "EXPAND"
  | "PIVOT"
  | "DEEPEN"
  | "ABANDON"
  | "VALIDATE"
  | "CHECKPOINT"
  | "CONCLUDE"
  | "HANDOFF_IR"
  // Written by the controller, never emitted. Deliberately absent from
  // DECISION_ACTIONS below, which is the vocabulary a lead may use: an arch
  | "STALLED";

// What a Hunt Lead may emit. A subset of DecisionAction, not the whole union.
export const DECISION_ACTIONS = [
  "INVESTIGATE",
  "EXPAND",
  "PIVOT",
  "DEEPEN",
  "ABANDON",
  "VALIDATE",
  "CHECKPOINT",
  "CONCLUDE",
  "HANDOFF_IR",
] as const satisfies readonly DecisionAction[];

// These rest on a judgement about existing evidence, so an uncited one is
// unauditable. EXPAND is here for a different reason: it names what to read.
export const ACTIONS_REQUIRING_CITATION: ReadonlySet<DecisionAction> = new Set([
  "ABANDON",
  "VALIDATE",
  "PIVOT",
  "EXPAND",
]);

// parked is the budget checkpoint: the hunt has stopped spending and waits on an
// operator to extend, conclude or abort it. It advances nothing until then, so it
export type HuntStatus = "pending_approval" | "active" | "parked" | "terminal";

// failed is an ending like the others, not an absence of one: a hunt stopped by a
// fault still has hypotheses to resolve and a report to write.
export type HuntOutcome =
  | "completed"
  | "budget_terminated"
  | "data_starved"
  | "failed"
  | "aborted";

// Higher wins. An outcome on the record is never downgraded, so a late
// "completed" cannot relabel a hunt that was aborted or ran out of budget.
export const OUTCOME_PRECEDENCE: Record<HuntOutcome, number> = {
  completed: 0,
  budget_terminated: 1,
  data_starved: 2,
  failed: 3,
  // Above failed: a run a person stopped is stopped for that reason.
  aborted: 4,
};

// handed_off is terminal for the hunt but not an ending of it: the claim now
// belongs to incident response, and the hunt carries on with the rest.
export type HypothesisStatus =
  | "active"
  | "proven"
  | "disproven"
  | "inconclusive"
  | "parked"
  | "handed_off";

export type Salience = "routine" | "notable" | "anomalous";
// "neither" is a first-class answer, not an omission: considered-and-irrelevant
// and never-considered are the difference the cross-hypothesis update exists for.
export type LinkRelation = "supports" | "weakens" | "neither";

// The harness counts model calls; a hunt counts turns, and one turn costs several.
export type Budgets = BudgetLimits & { max_iterations: number };

// A turn dispatches up to max_workers workers, each running its own tool loop, plus
// the lead and the pass that argues against them; the two spare turns per agent are the
// emission retries. Derived rather than guessed, since a flat constant undercounts a
// fan-out. Cost is the governor; calls only backstop a turn running away.
export function callsPerIteration(maxWorkers: number, maxTurns: number): number {
  return (maxWorkers + 2) * (maxTurns + 2);
}

// The shape the shipped arch and runtime describe: 4 workers, 8 turns each.
export const CALLS_PER_ITERATION = callsPerIteration(4, 8);

// 90 minutes: a hunt averages several minutes an iteration once workers fan out, and
// a wall that fires first makes every other budget decorative.
export const DEFAULT_BUDGETS: Budgets = {
  max_iterations: 8,
  max_calls: 8 * CALLS_PER_ITERATION,
  max_cost_usd: 15.0,
  max_wall_ms: 5_400_000,
  max_park_ms: DEFAULT_PARK_MS,
};

// Closed, so a typo in an extraction key pattern cannot invent a type, and a
// seed entity lands in the same namespace as everything the graph extracts.
export const ENTITY_TYPES = [
  "ip",
  "domain",
  "host",
  "url",
  "email",
  "hash",
  "arn",
  "aws_key",
  "user",
  "process",
] as const;

export type EntityType = (typeof ENTITY_TYPES)[number];

export interface Entity {
  type: EntityType;
  value: string;
}

// Human input: the one thing on the ledger that is direction rather than data, applied
// at an iteration boundary. An array, because another process must check it at runtime.
export const DIRECTIVE_KINDS = [
  "note",
  "lead",
  "abort",
  "extend",
  "conclude",
  "approve",
  "reject",
  "benign",
  "gap",
  "boost",
] as const;

export type DirectiveKind = (typeof DIRECTIVE_KINDS)[number];

// What an extend buys. Parsed from the operator's text when the directive is
// queued, so the ledger records the ask as numbers rather than prose the drain
export interface BudgetGrant {
  iterations: number;
  cost_usd: number;
  // Minutes of wall clock. On offer because iterations and dollars left over are no
  // use to a run whose clock ran out.
  wall_ms: number;
}

export interface Directive {
  directive_id: string;
  actor: string;
  kind: DirectiveKind;
  text: string;
  created_at: string;
  grant?: BudgetGrant;
  // Which checkpoint an approve or a reject answers. Typed rather than parsed
  // out of the text, so an answer can never land on the wrong question.
  checkpoint_id?: string;
  // What the soft set names: the entity a benign suppresses, the lead a boost
  // pins, the hypothesis a declared gap bears on.
  entity_key?: string;
  question_id?: string;
  hypothesis_id?: string;
  // The tenant a lead is asking about, typed rather than grepped out of prose.
  // The regex over the text stays as a fallback for a line written into the
  tenant?: string;
  // Set on the benign that lifts an earlier one. Reversal is an append like
  // everything else — the suppression it undoes stays on the record.
  revoke?: boolean;
  // Set on notes the controller journals itself, so the report can say whose voice a
  // line is. The drain excludes by id and never reads it.
  origin?: "inbox" | "controller";
}

export interface HuntState {
  hunt_id: string;
  name: string;
  // Resolved once at hunt start: resume needs no YAML, and editing an arch file
  // mid-run cannot silently change what a hunt in flight was told.
  spec: HuntSpec;
  // Journaled so stochastic resurfacing replays exactly, on resume and on audit.
  seed: string;
  status: HuntStatus;
  outcome: HuntOutcome | null;
  iteration: number;
  cost_usd: number;
  budgets: Budgets;
  scope: Record<string, unknown>;
  narrative: string;
  created_at: string;
  terminated_at: string | null;
  // When the budget checkpoint parked the hunt, and what it said. The park TTL is
  // measured from parked_at, lazily, whenever the hunt is next touched.
  parked_at: string | null;
  parked_reason: string | null;
  // Why the hunt ended, when the ending was not a Hunt Lead decision: a predicate
  // that passed, an operator's directive, a park that expired.
  termination_reason: string | null;
}

export interface Hypothesis {
  hypothesis_id: string;
  statement: string;
  status: HypothesisStatus;
  attack_technique: string | null;
  provenance: string;
  resolution_reason: string | null;
  // What the numbers were at verdict time. Absent while the hypothesis is
  // active; a verdict that cannot be re-read is not auditable.
  evidence_strength?: EvidenceStrength | null;
  // The IR case this claim was escalated into. Local to this app — the ADR
  // traded away the Vigil Case foreign key — so it names a case file beside the
  spawned_case_id?: string | null;
}

// HANDOFF_IR, journaled. The hunt keeps running after one: an escalation moves a
// claim to another team, it does not end the investigation that found it.
export interface Handoff {
  case_id: string;
  hypothesis_id: string;
  iteration: number;
  rationale: string;
  // The rendered document is journaled rather than written beside a ledger that
  // no longer has a path. case_file survives only on ledgers written before that.
  case_markdown?: string;
  case_file?: string;
  created_at: string;
}

// Controller-computed from deterministic features of the ledger, never a model
// self-report, and the only thing a verdict is allowed to gate on.
export interface EvidenceStrength {
  corroborating_sources: number;
  contradicting_records: number;
  open_gaps: number;
  attacker_influenceable_only: boolean;
  survived_disconfirmation: boolean;
}

// A lead on the frontier. The three provenance fields are facts of capture and
// are stored; everything the priority score derives from them is computed on read.
export interface OpenQuestion {
  question_id: string;
  question: string;
  // closed means a worker took it; parked means the hunt ended below the priority
  // floor and it became backlog. Distinct statuses because a lead nobody ever
  status: "open" | "closed" | "parked";
  // The entity this lead is about, so a worker taking it is told what to look at.
  entity_key: string | null;
  // Set when a decision cited one record; a worker's follow-up names its dispatch
  // instead, because attributing it to a single record would be invented provenance.
  spawning_evidence_id: string | null;
  spawning_dispatch_id: string | null;
  spawned_iteration: number;
  // The hypothesis this lead was opened in service of. Without it a lead that
  // fails is a gap belonging to nothing, and no hypothesis is ever gap-locked.
  hypothesis_id: string | null;
  // Why it left the frontier, carrying the score it was parked at. Optional
  // because a lead closed by the worker that took it needs no explanation.
  closed_reason?: string | null;
}

// Who chose the value a finding rests on. The adversary picks a filename, a
// User-Agent, a process name; they cause a beacon interval and a connection count but
// cannot author what the sensor counted without owning the sensor. A third party's
// claim -- a feed label, a reputation verdict -- is nobody's observation.
export type Authorship = "sensor" | "adversary" | "third_party";

// One thing a finding rests on. Named so a verdict can ask what the support actually
// stands on rather than whether the adversary touched the record at all: in a hunt the
// adversary's own behaviour is the signal, so attacker-caused is every record there is.
export interface RestsOn {
  field: string;
  authored: Authorship;
}

export interface EvidenceRecord {
  evidence_id: string;
  dispatch_id: string | null;
  iteration: number;
  source_system: string;
  summary: string;
  payload: Record<string, unknown>;
  salience: Salience;
  why_notable: string;
  provenance: string;
  // The worker's own claim about this record, distinct from the playbook author's
  // label. Absent rather than null, and schema-gated so one cannot be invented.
  attack_technique?: string;
  // Set when an adversary could have written the value. Superseded by rests_on, which
  // says which values: kept because the ledger holds it and a run written before the
  // split still reads correctly.
  attacker_influenceable: boolean;
  // What the finding rests on, field by field. Absent on a ledger written before the
  // split, and on a worker that named nothing.
  rests_on?: RestsOn[];
  instruction_like: boolean;
  // Extracted once at capture and stored, so tightening the pattern later cannot
  // rewrite the graph a past decision was made against.
  entities: Entity[];
  captured_at: string;
}

export interface EvidenceLink {
  evidence_id: string;
  hypothesis_id: string;
  relation: LinkRelation;
}

// One tool invocation and what came back, capped. The execution log the audit
// trail needs: a summary is the worker's account of the data, this is the data.
export interface ToolCall {
  tool: string;
  arguments: string;
  result: string;
}

export interface DispatchRecord {
  dispatch_id: string;
  iteration: number;
  agent_id: string;
  status: "pending" | "complete" | "failed";
  query_intent: string;
  target_hypothesis_id: string | null;
  // The lead this dispatch took, so an interrupted one can hand it back.
  question_id: string | null;
  failure_reason: string | null;
  // What the worker spent and what it ran. Both land on the completion patch,
  // since the row is journaled before the worker starts.
  cost_usd: number;
  calls: ToolCall[];
}

export interface Decision {
  action: DecisionAction;
  rationale: string;
  // Recorded for calibration only. Nothing gates on it.
  stated_confidence?: number | null;
  evidence_citations?: string[];
  target_hypothesis_id?: string | null;
  // An entity key the graph already knows. With target_hypothesis_id it is the
  // focus, which is what makes DEEPEN and PIVOT distinguishable.
  target_entity?: string | null;
  worker_agent_id?: string | null;
  // Nullable like its siblings: a verb carrying no query emits null, and the
  // controller falls back to the rationale.
  query_intent?: string | null;
  // How the evidence of the previous iteration bears on every active hypothesis.
  // The lead cannot classify what its own dispatch has not returned yet.
  evidence_relations?: EvidenceRelation[];
}

export interface EvidenceRelation {
  evidence_id: string;
  hypothesis_id: string;
  relation: LinkRelation;
}

// What the hunt is currently looking at. Derived from the decisions, never stored.
export interface Focus {
  entity: string | null;
  hypothesis: string | null;
}

export interface DecisionResult {
  decision: Decision;
  model_id: string;
  prompt_version: string;
  cost_usd: number;
  // Emissions the controller rejected before accepting one, kept so re-prompts stay visible.
  rejected_attempts?: string[];
}

export interface DecisionRecord extends DecisionResult {
  decision_id: string;
  iteration: number;
  digest_presented: Digest;
  // Events on the ledger when the digest was built, so replay folds exactly
  // log[0..digest_seq). The decision is journaled after its own dispatches are,
  digest_seq?: number;
  created_at: string;
}

export interface HypothesisView {
  hypothesis_id: string;
  statement: string;
  status: HypothesisStatus;
}

export interface EvidenceView {
  evidence_id: string;
  source_system: string;
  summary: string;
  salience: Salience;
  why_notable: string;
  instruction_like: boolean;
}

export interface EntityView {
  type: EntityType;
  value: string;
  count: number;
  first_evidence_id: string;
  // An operator called it known-benign. Annotated rather than hidden: the
  // records that mention it are untouched, and a hunt that silently dropped an
  suppressed?: boolean;
}

// A raw payload the lead asked for by id. Rendered delimited, like all evidence.
export interface Expansion {
  evidence_id: string;
  payload: string;
}

export interface Digest {
  hunt_id: string;
  hunt_name: string;
  iteration: number;
  narrative: string;
  hypotheses: HypothesisView[];
  recent_evidence: EvidenceView[];
  // Strongest counter-evidence per active hypothesis; one-sidedness is itself a finding.
  weakens: Record<string, EvidenceView[]>;
  // What the hunt has touched. PIVOT changes the entity, DEEPEN keeps it, so the
  // lead cannot tell the two apart without seeing them.
  entities: EntityView[];
  focus: Focus;
  // Entities adjacent to the focus in the graph, so a PIVOT names something the
  // evidence has actually seen rather than inventing a value.
  pivot_candidates: EntityView[];
  // Routine records the window dropped. Named rather than discarded, so the lead
  // knows they exist and can EXPAND one.
  omitted: { count: number; evidence_ids: string[] };
  expansions: Expansion[];
  open_questions: string[];
  budget_remaining: { iterations: number; cost_usd: number };
  // Operator instructions. Unlike evidence, these are direction.
  directives: string[];
  notes: string[];
}

export interface DispatchRequest {
  dispatch_id: string;
  hunt_id: string;
  agent_id: string;
  query_intent: string;
  // The one lead or hypothesis this worker owns when an iteration fans out.
  focus: string;
  target_hypothesis_id: string | null;
  scope: Record<string, unknown>;
  // Cancelled when an operator halts the hunt mid-query. Not journaled — it is
  // a handle on work in flight, and what it cancels is recorded as a gap like
  signal?: AbortSignal;
}

// supports/weakens name the hypotheses this record bears on; the controller
// turns them into links, so a worker still never writes state itself. entities
export type WorkerEvidence = Omit<
  EvidenceRecord,
  "evidence_id" | "dispatch_id" | "iteration" | "entities" | "captured_at"
> & { supports?: string[]; weakens?: string[] };

export interface DispatchResult {
  dispatch_id: string;
  evidence: WorkerEvidence[];
  // New threads the work opened up — the frontier of the search.
  questions?: string[];
  failed: boolean;
  failure_reason: string;
  // Required, including on the failure path: a worker that burned tokens and
  // then died still spent them, and hunt.cost_usd is the budget counter.
  cost_usd: number;
  calls?: ToolCall[];
}

export interface NullCheckEvidence {
  relation: LinkRelation;
  record: EvidenceRecord;
}

// What the disconfirmation critic is given: the claim and the raw payloads
// behind it. Deliberately not the digest — the digest is the Hunt Lead's own
export interface NullCheckInput {
  hypothesis_id: string;
  statement: string;
  narrative: string;
  evidence: NullCheckEvidence[];
}

export interface NullCheckResult {
  // Whether the hypothesis is left standing. false means the benign explanation
  // accounts for the evidence, so nothing here has been shown.
  survives: boolean;
  strongest_benign_explanation: string;
  rationale: string;
  cost_usd: number;
  model_id: string;
  prompt_version: string;
}

export interface IterationResult {
  hunt_id: string;
  iteration: number;
  action: DecisionAction;
  decision_id: string;
  cost_usd: number;
  evidence_appended: number;
  // Records the controller enriched in on its own, spending no decision on them.
  enriched: number;
  hunt_status: HuntStatus;
  hunt_outcome: HuntOutcome | null;
  note: string;
}
