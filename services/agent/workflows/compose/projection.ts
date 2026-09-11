import type { AgentEvent, DispatchPayload } from "../../contracts/events.js";

// Gated execute traces only. Phase-level dispatches carry no result.
export interface ComposeProjection {
  run_id: string;
  results: unknown[];
}

export function composeProjection(
  runId: string,
  events: readonly AgentEvent<Record<never, never>>[],
): ComposeProjection {
  const results: unknown[] = [];
  for (const event of events) {
    if (event.kind !== "dispatch") continue;
    const payload = event.payload as DispatchPayload;
    if (payload.result === undefined) continue;
    results.push(payload.result);
  }
  return { run_id: runId, results };
}
