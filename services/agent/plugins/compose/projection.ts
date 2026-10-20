import type { AgentEvent, DispatchPayload } from "../../contracts/events.js";
import { agentProjection, type AgentProjection } from "../../core/agent-view.js";
import type { ComposeKinds } from "./vocabulary.js";

// The kernel's own fold (status, outcome, open checkpoint, cost, unbound tools), so a
// supervisor can tell a compose run that ended or is parked from one still going.
// Plus the gated execute traces; phase-level dispatches carry no result.
export interface ComposeProjection extends AgentProjection {
  run_kind: "compose";
  results: unknown[];
}

export function composeProjection(runId: string, events: readonly AgentEvent<ComposeKinds>[]): ComposeProjection {
  const results: unknown[] = [];
  for (const event of events) {
    if (event.kind !== "dispatch") continue;
    const payload = event.payload as DispatchPayload;
    if (payload.result === undefined) continue;
    results.push(payload.result);
  }
  return { ...agentProjection(runId, events), run_kind: "compose", results };
}
