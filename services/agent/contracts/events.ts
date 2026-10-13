// One of the four Phase-0 contracts. Consumed by the ledger and resume paths,
// the hunt workflow, the checkpoint mirror, and Python's two permitted reads.

import type { BudgetLimits, SpendPayload } from "./budget.js";
import type { RecallPayload } from "./memory.js";

// Adding a kind is never a migration: kind is text in the table and validated here
// against a closed union. Changing an existing kind's payload bumps this.
export const EVENT_SCHEMA_VERSION = 1;

// tally is the conformance workflow, not a product surface: it keeps the harness
// boundary exercised by something that is not a real domain.
// root-cause reuses the hunt loop (its arch entry declares workflow: "hunt"): a
// hunt confirms that a threat exists, root-cause works backward from a confirmed
// one to how it got there. Same machinery, its own kind so it is never mislabelled.
// adjudicate is the same loop again, framed as a shadow second opinion on a finding
// intake already admitted: it proposes a workflow and executes nothing.

// subagent is a child run: a judge or helper a run started under its own prompt,
// journaled on a ledger of its own and linked to its parent. Never enqueued.
export const RUN_KINDS = ["hunt", "root_cause", "adjudicate", "investigate", "compose", "chat", "tally", "subagent"] as const;
export type RunKind = (typeof RUN_KINDS)[number];

// Domain-free, so the harness never imports a workflow's vocabulary.
// A workflow declares its own kinds and the ledger repository is generic over them.
export const RUN_EVENT_KINDS = [
  "run",
  "spend",
  "recall",
  "dispatch",
  "checkpoint",
  "resolution",
  "directive",
  "patch",
  "resumed",
  "terminal",
  // The kernel's own: a tool call and its result, the answer a run ends on and its
  // second look. Every plugin writes them, so one projection, replay and distil read all.
  "call",
  "conclusion",
  "review",
  // What each model call was shown, by hash, and the bytes behind each hash once
  // per run: what a replay rebuilds the call's input from and checks it against.
  "frame",
  "content",
  // A judge or subagent run under its own ledger, linked here when it ends.
  "child",
  // The hypothesis record: what the run believes, what bears on it, and what it
  // has not been able to answer. Every plugin may keep one; some must.
  "hypothesis",
  "link",
  "question",
] as const;
export type RunEventKind = (typeof RUN_EVENT_KINDS)[number];

// abandoned is not aborted: aborted means a human stopped the run, and a run
// nobody answered before its park TTL is the opposite -- nobody decided anything.
export const RUN_OUTCOMES = ["completed", "failed", "aborted", "budget_exhausted", "abandoned"] as const;
export type RunOutcome = (typeof RUN_OUTCOMES)[number];

// seq 0. Carries the resolved spec so resume needs no configuration file and
// editing an arch mid-run cannot change a run already in flight.
export interface RunPayload {
  run_kind: RunKind;
  spec: unknown;
  budgets: BudgetLimits;
  seed: string;
  tenant_id: string | null;
  started_by: string;
  // Set on a child run: the run it was started by, and what for.
  parent?: ParentLink;
}

export interface ParentLink {
  run_id: string;
  run_kind: RunKind;
  role: string;
  key: string;
}

export interface DispatchPayload {
  dispatch_id: string;
  agent_id: string;
  status: "pending" | "complete" | "failed";
  question_id: string | null;
  failure_reason: string | null;
  // Optional because the harness reads none of them: a dispatch is written by a
  // workflow, and which of these it fills is that workflow's business.
  iteration?: number;
  query_intent?: string;
  target_hypothesis_id?: string | null;
  cost_usd?: number;
  calls?: unknown[];
  // What a gated call returned. Written by the harness and by nothing else, and
  // what makes an approved call run once however many times the run resumes.
  result?: unknown;
}

// A capability, or a phase's tool, this deployment cannot answer: journalled once
// when the run opens so a blind spot is a ledger fact rather than a log line.
export interface UnboundPayload {
  capability: string;
  reason: string;
  phase_id?: string;
}

// checkpoint_class and directive kind are workflow vocabulary, so they stay
// strings here; the closed set for each lives in the workflow that declares it.
export interface CheckpointPayload {
  checkpoint_id: string;
  checkpoint_class: string;
  question: string;
  raised_at: string;
  // What a workflow needs to answer its own checkpoint. Optional because the
  // harness raises checkpoints too, and its approval gate carries neither.
  raised_iteration?: number;
  context?: Record<string, unknown>;
}

// The one a resolution must answer for a run to go on -- the only question a
// supervisor actually asks of one, so every projection reports it the same way.
export interface OpenCheckpoint {
  checkpoint_id: string;
  checkpoint_class: string;
  question: string;
  raised_at: string;
  context: Record<string, unknown>;
}

export function openCheckpoint(checkpoint: CheckpointPayload): OpenCheckpoint {
  const { checkpoint_id, checkpoint_class, question, raised_at } = checkpoint;
  return { checkpoint_id, checkpoint_class, question, raised_at, context: checkpoint.context ?? {} };
}

// The resolution event is what unblocks a run, and nothing else does.
export interface ResolutionPayload {
  checkpoint_id: string;
  actor: string;
  answer: "approve" | "reject";
  text: string;
  resolved_at: string;
  // The operator directive that carried it, when a human answered. Absent on a
  // policy resolution: there was no input, only a rule.
  directive_id?: string | null;
}

export interface DirectivePayload {
  directive_id: string;
  actor: string;
  kind: string;
  text: string;
  created_at: string;
}

export interface PatchPayload {
  target: string;
  id: string;
  fields: Record<string, unknown>;
}

// Its own kind rather than a patch to run status, so Python can report an
// outcome with one indexed query and never reimplements the fold.
// A run picked back up. Nothing marked one, so a run that died and recovered read
// as one that never stopped: the gap in the record had no explanation in it.
// Written only where the run then made progress, so a parked run swept every
// interval leaves one mark rather than one per sweep.
export interface ResumedPayload {
  worker: string;
  // Who put it back on the queue -- the watchdog, the console, an answered
  // checkpoint. Which of those it was is the whole question a reader is asking.
  enqueued_by: string;
}

export interface TerminalPayload {
  outcome: RunOutcome;
  reason: string;
  // What the run leaves behind. Written by the workflow, because only it knows
  // what its deliverable is, and read from here by whoever reports the run out.
  summary?: string;
  handoffs?: TerminalHandoff[];
}

// Work the run finished by giving away, each carrying the document it hands over
// rather than a pointer to one: the receiver has no access to this ledger.
export interface TerminalHandoff {
  case_id: string;
  title: string;
  markdown: string;
}

// What a tool result was: served (an answer, empty or not), visibility_gap (the estate did
// not answer: a blind spot) or defect (the call was wrong: never recorded as a gap).
export const CALL_CLASSES = ["served", "visibility_gap", "defect"] as const;
export type CallClass = (typeof CALL_CLASSES)[number];

// One tool call, journaled the moment its result is in: what a resume rebuilds the
// transcript from and what a reader cites. ref is the id the model was shown.
export interface CallPayload {
  ref: string;
  // The model turn that asked for it, counted from 1 across resumes.
  turn: number;
  call_id: string;
  tool: string;
  arguments: string;
  // What the model read: scrubbed, capped and delimited.
  result: string;
  ok: boolean;
  failure: string | null;
  // Absent only on ledgers written before calls were classified; classOf reads those.
  class?: CallClass;
  // What the model said before asking, on the first call of its turn only.
  thought?: string;
  duration_ms?: number;
  // The role that made the call, when it was not the run's one agent: a step a
  // plugin ran under its own prompt and grants.
  role?: string;
}

// The answer the run ends on, in the shape its plugin declared.
export interface ConclusionPayload {
  plugin: string;
  value: Record<string, unknown>;
  // True when the turn cap, not the model, ended the tool loop.
  capped: boolean;
  turns: number;
  cost_usd: number;
  // What the run could not settle, off the ledger: open questions, what its answer said it
  // could not settle, and every call the estate failed. Absent on older ledgers.
  declared_gaps?: {
    open_questions: string[];
    declared: string[];
    visibility: { ref: string; tool: string; failure: string | null }[];
  };
}

// The second look: refs the conclusion cited that no step carries are counted
// here, deterministically, whatever the reviewer said.
export interface ReviewPayload {
  stance: "uphold" | "overturn" | "insufficient" | "skipped";
  reasons: string;
  missing: string[];
  cited: string[];
  unknown_refs: string[];
  // Set when the plugin guards a field: the value it asked for, and the value the
  // run ends on once the guard has been applied.
  guarded?: { field: string; asked: string; final: string };
  // Set when the reviewer itself could not answer.
  failure?: string;
  // Set when the plugin requires a record: whether it links a cited, served result
  // to a hypothesis. A guarded answer with no such link does not stand.
  grounded?: boolean;
  // The child run the reviewer ran as.
  child_run_id?: string;
}

// One model call's input, content-addressed. Every hash names a content event on
// this ledger, so a replay rebuilds the input from the ledger alone and diffs it.
export interface FramePayload {
  role: string;
  // Model calls by this role in this run, counted from 1 across resumes.
  call: number;
  model_id: string;
  provider_type: string;
  sampling: Record<string, unknown>;
  system: string;
  task: string;
  // The rendered recall block, which the recall event's rows render to.
  recall: string;
  tools: { id: string; hash: string }[];
  // Every skill the plugin offers, by the hash of the body load_skill would return.
  skills: { name: string; hash: string }[];
  // The calls whose results the input carries, in order, by ref. Messages from
  // before the run -- a conversation's own history -- are hashed in history.
  transcript: string[];
  history: string | null;
  // How the transcript was trimmed: the fold policy, how many messages it folded, and the
  // model window's lower ceiling when set (#2125). Deterministic, so a replay folds the same.
  fold: { policy: Record<string, number> | null; folded: number; ceiling?: number };
  // The transient ask after the transcript, and the answer's schema, on an emission.
  tail: string | null;
  emit: string | null;
  // The hash of the whole request: messages, tool schemas and emission schema.
  input: string;
}

export interface ContentPayload {
  hash: string;
  text: string;
}

// The parent's end of a child run's link: written when the child has ended.
export interface ChildPayload {
  child_run_id: string;
  role: string;
  key: string;
  outcome: RunOutcome | "waiting_approval";
  reason: string;
  cost_usd: number;
}

// The hypothesis record's kernel shape. A plugin may write a richer one under the
// same kind (the hunt's carries its verdict machinery); every field here is in it.
export interface HypothesisPayload {
  hypothesis_id: string;
  statement: string;
  status: string;
  provenance: string;
  resolution_reason: string | null;
}

// Evidence bearing on a hypothesis. evidence_id is a call ref in a plain run and
// an evidence record in a run that keeps its own.
export interface LinkPayload {
  evidence_id: string;
  hypothesis_id: string;
  relation: "supports" | "weakens" | "neither";
}

export interface QuestionPayload {
  question_id: string;
  question: string;
  status: "open" | "closed" | "parked";
  hypothesis_id: string | null;
  closed_reason?: string | null;
}

export interface RunEventPayloads {
  run: RunPayload;
  spend: SpendPayload;
  // The read of episodic memory the run opened on, verbatim. Journaled because
  // the prefix carries the rows but nothing else records them, and a rebuild that
  // re-reads memory reads a neighbourhood that has moved since (ADR 0015).
  recall: RecallPayload;
  dispatch: DispatchPayload;
  checkpoint: CheckpointPayload;
  resolution: ResolutionPayload;
  directive: DirectivePayload;
  patch: PatchPayload;
  resumed: ResumedPayload;
  terminal: TerminalPayload;
  call: CallPayload;
  conclusion: ConclusionPayload;
  review: ReviewPayload;
  frame: FramePayload;
  content: ContentPayload;
  child: ChildPayload;
  hypothesis: HypothesisPayload;
  link: LinkPayload;
  question: QuestionPayload;
}

// snapshot holds the digest presented to the lead. Selected only by replay and
// never by the fold: decision events reach 56.7 KB and a long run tens of MB.
export interface EventEnvelope<K extends string, P> {
  run_id: string;
  run_kind: RunKind;
  seq: number;
  ts: string;
  kind: K;
  payload: P;
  snapshot?: unknown;
  schema_version: number;
}

type EventsOf<M> = { [K in keyof M & string]: EventEnvelope<K, M[K]> }[keyof M & string];

export type RunEvent = EventsOf<RunEventPayloads>;

// What a workflow's ledger holds: the domain-free kinds plus its own. A kind the
// workflow declares itself is read in its shape, not the kernel's.
export type AgentEvent<M> = Exclude<RunEvent, { kind: keyof M }> | EventsOf<M>;

// What is appended. seq and ts are assigned by the repository, so no caller can
// choose its own position in the log.
export type NewEvent<M> = Omit<AgentEvent<M>, "seq" | "ts" | "schema_version">;
