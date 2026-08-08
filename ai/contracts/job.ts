// Contract 4 of 5 (issue #589). Consumed by WS-B (#597 resume), WS-E (#594
// deployment) and the Python backend, which enqueues plain JSON (ADR-0001).

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
}

// A resume carries no request, so a resume path that read one would not compile.
// That is the "resumable from the payload plus the ledger" guarantee, as a type.
export type RunJob =
  | (JobBase & { reason: "start"; request: StartRequest })
  | (JobBase & { reason: "resume" });

// jobId = run_id for a start, so a double POST dedupes in BullMQ rather than in
// application code; a resume is per ledger position for the same reason.
export function jobIdFor(job: RunJob, seq?: number): string {
  return job.reason === "start" ? job.run_id : `${job.run_id}:${seq ?? 0}`;
}
