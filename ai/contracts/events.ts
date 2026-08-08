// Contract 2 of 5 (issue #589). Consumed by WS-B (#591 ledger, #597 resume),
// WS-D (#596 hunt), WS-F (#599 mirror) and the Python read path in #590.

import type { BudgetLimits, SpendPayload } from "./budget.js";

export const EVENT_SCHEMA_VERSION = 1;

export const RUN_KINDS = ["hunt", "investigate", "compose", "chat"] as const;
export type RunKind = (typeof RUN_KINDS)[number];

// Domain-free (ADR-0002), so the harness never imports a workflow's vocabulary.
// A workflow declares its own kinds and the ledger repository is generic over them.
export const RUN_EVENT_KINDS = [
  "run",
  "spend",
  "dispatch",
  "checkpoint",
  "resolution",
  "directive",
  "patch",
  "terminal",
] as const;
export type RunEventKind = (typeof RUN_EVENT_KINDS)[number];

export const RUN_OUTCOMES = ["completed", "failed", "aborted", "budget_exhausted"] as const;
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
}

// checkpoint_class and directive kind are workflow vocabulary, so they stay
// strings here; the closed set for each lives in the workflow that declares it.
export interface CheckpointPayload {
  checkpoint_id: string;
  checkpoint_class: string;
  question: string;
  raised_at: string;
}

// The resolution event is what unblocks a run, and nothing else does (ADR-0003).
export interface ResolutionPayload {
  checkpoint_id: string;
  actor: string;
  answer: "approve" | "reject";
  text: string;
  resolved_at: string;
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
export interface TerminalPayload {
  outcome: RunOutcome;
  reason: string;
}

export interface RunEventPayloads {
  run: RunPayload;
  spend: SpendPayload;
  dispatch: DispatchPayload;
  checkpoint: CheckpointPayload;
  resolution: ResolutionPayload;
  directive: DirectivePayload;
  patch: PatchPayload;
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
