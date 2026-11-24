import type { AgentEvent, DispatchPayload } from "../../contracts/events.js";
import { agentProjection, type AgentProjection } from "../../core/agent-view.js";
import type { ComposeKinds } from "./vocabulary.js";

// One emulator call's journaled answer, by its dispatch id.
export interface Emulated {
  dispatch_id: string;
  tool: string;
  result: unknown;
}

// The kernel's own fold (status, outcome, open checkpoint, cost, unbound tools), so a
// supervisor can tell a compose run that ended or is parked from one still going.
// Plus the gated execute traces; phase-level dispatches carry no result. emulated is
// the stamped answers of tools the deployment marks, the only ones whose steps score.
export interface ComposeProjection extends AgentProjection {
  run_kind: "compose";
  results: unknown[];
  emulated: Emulated[];
}

export function composeProjection(runId: string, events: readonly AgentEvent<ComposeKinds>[]): ComposeProjection {
  const results: unknown[] = [];
  // A resumed run serves an approved call from the ledger and journals it again: once each.
  const emulated = new Map<string, Emulated>();
  for (const event of events) {
    if (event.kind !== "dispatch") continue;
    const payload = event.payload as DispatchPayload;
    if (payload.result === undefined) continue;
    results.push(payload.result);
    if (payload.emulator === true && payload.tool !== undefined) {
      emulated.set(payload.dispatch_id, { dispatch_id: payload.dispatch_id, tool: payload.tool, result: payload.result });
    }
  }
  return { ...agentProjection(runId, events), run_kind: "compose", results, emulated: [...emulated.values()] };
}
