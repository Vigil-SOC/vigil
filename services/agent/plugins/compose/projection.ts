import type { AgentEvent, DispatchPayload, UnboundPayload } from "../../contracts/events.js";
import type { ComposeKinds } from "./vocabulary.js";

// Gated execute traces only, and the tools a phase asked for that this deployment
// lacks. Phase-level dispatches carry no result.
export interface ComposeProjection {
  run_id: string;
  run_kind: "compose";
  results: unknown[];
  unbound: UnboundPayload[];
}

export function composeProjection(runId: string, events: readonly AgentEvent<ComposeKinds>[]): ComposeProjection {
  const results: unknown[] = [];
  const unbound: UnboundPayload[] = [];
  for (const event of events) {
    if (event.kind === "unbound") unbound.push(event.payload as UnboundPayload);
    if (event.kind !== "dispatch") continue;
    const payload = event.payload as DispatchPayload;
    if (payload.result === undefined) continue;
    results.push(payload.result);
  }
  return { run_id: runId, run_kind: "compose", results, unbound };
}
