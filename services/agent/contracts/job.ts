// One of the four Phase-0 contracts. Consumed by the resume path, deployment,
// and the Python backend, which enqueues plain JSON and writes no ledger.

import type { RunKind } from "./events.js";

export const JOB_SCHEMA_VERSION = 1;

// No colon: BullMQ's Node library refuses a queue name containing one, while
// its Python library accepts it and writes the keys anyway. Keys are bull:agent-runs:*.
export const RUN_QUEUE = "agent-runs";

interface JobBase {
  schema_version: number;
  run_id: string;
  run_kind: RunKind;
  tenant_id: string | null;
  enqueued_at: string;
  enqueued_by: string;
}

// References rather than resolved content: the worker resolves them and journals
// the result into the run event, so Python never writes the ledger (D2).
export interface StartRequest {
  arch: string;
  playbook: string;
  config: string;
  prompt: string;
  overrides?: Record<string, unknown>;
  // What the caller wants tested, beside what the playbook states. Per-run, so it is
  // not resolvable from the reference.
  hypotheses?: string[];
  // How many turns this run may take. Per-run for the same reason; absent leaves the
  // config's.
  iterations?: number;
  // Whether a person approves the hypotheses before the hunt spends anything. The policy
  // defaults to auto, so a headless run advances with nobody to ask.
  approve_hypotheses?: boolean;
}

// A resume carries no request, so a resume path that read one would not compile.
// That is the "resumable from the payload plus the ledger" guarantee, as a type.
export type RunJob =
  | (JobBase & { reason: "start"; request: StartRequest })
  | (JobBase & { reason: "resume" });

// jobId = run_id for a start, so a double POST dedupes in BullMQ. A resume takes a
// fresh id and does not dedupe: run-level exclusion is the lease's, which is stronger.
//
// Joined with a dash for the same reason RUN_QUEUE holds no colon: BullMQ refuses a
// custom id containing one, and the sweeper's add is the only caller -- a throw there
// means no parked or crashed run is ever resumed.
export function jobIdFor(job: RunJob, attempt: string): string {
  return job.reason === "start" ? job.run_id : `${job.run_id}-${attempt}`;
}
