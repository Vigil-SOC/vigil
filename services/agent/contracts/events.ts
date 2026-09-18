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
export const RUN_KINDS = ["hunt", "root_cause", "adjudicate", "investigate", "compose", "chat", "tally"] as const;
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

// What a workflow's ledger holds: the domain-free kinds plus its own.
export type AgentEvent<M> = RunEvent | EventsOf<M>;

// What is appended. seq and ts are assigned by the repository, so no caller can
// choose its own position in the log.
export type NewEvent<M> = Omit<AgentEvent<M>, "seq" | "ts" | "schema_version">;

export function isRunEventKind(kind: string): kind is RunEventKind {
  return (RUN_EVENT_KINDS as readonly string[]).includes(kind);
}
