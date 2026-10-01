import type { SpendPayload } from "../../contracts/budget.js";
import { openCheckpoint, type AgentEvent, type CheckpointPayload, type DispatchPayload, type OpenCheckpoint, type ResolutionPayload, type TerminalPayload } from "../../contracts/events.js";
import { recalledPayloadOf, type RecallPayload } from "../../contracts/memory.js";
import { callViews, type CallView } from "../call-view.js";
import type { DecisionPayload, FindingPayload, LeadKinds } from "./workflow.js";

// What a reader outside this process is told about a run -- deliberately not the
// ledger, so a caller reads what a run decided rather than how it was written down.
export interface LeadProjection {
  run_id: string;
  status: "running" | "waiting_approval" | "terminal";
  outcome: TerminalPayload["outcome"] | null;
  reason: string;
  iterations: number;
  dispatched: number;
  cost_usd: number | null;
  decisions: DecisionPayload[];
  findings: FindingPayload[];
  // Null while nothing is parked, which is what a supervisor is actually asking.
  open_checkpoint: OpenCheckpoint | null;
  // Every question a dispatch asked, in ledger order.
  calls: CallView[];
  // The opening recall, off the ledger. Null when the run never asked.
  recall: RecallPayload | null;
}

export function leadProjection(runId: string, events: readonly AgentEvent<LeadKinds>[]): LeadProjection {
  const decisions: DecisionPayload[] = [];
  const findings: FindingPayload[] = [];
  const dispatches: DispatchPayload[] = [];
  const raised: CheckpointPayload[] = [];
  const answered = new Set<string>();
  let dispatched = 0;
  let terminal: TerminalPayload | null = null;
  // Null until a gateway prices something. A run that has spent nothing yet and
  // one whose provider reports no cost are different claims.
  let cost: number | null = null;

  for (const event of events) {
    switch (event.kind) {
      case "decision":
        decisions.push(event.payload as DecisionPayload);
        break;
      case "finding":
        findings.push(event.payload as FindingPayload);
        break;
      case "dispatch":
        dispatched += 1;
        dispatches.push(event.payload as DispatchPayload);
        break;
      case "checkpoint":
        raised.push(event.payload as CheckpointPayload);
        break;
      case "resolution":
        answered.add((event.payload as ResolutionPayload).checkpoint_id);
        break;
      case "spend": {
        const spent = (event.payload as SpendPayload).cost_usd;
        if (spent !== null) cost = (cost ?? 0) + spent;
        break;
      }
      case "terminal":
        terminal = event.payload as TerminalPayload;
        break;
    }
  }

  const open = raised.find((checkpoint) => !answered.has(checkpoint.checkpoint_id)) ?? null;
  return {
    run_id: runId,
    status: terminal !== null ? "terminal" : open !== null ? "waiting_approval" : "running",
    outcome: terminal?.outcome ?? null,
    reason: terminal?.reason ?? "",
    iterations: decisions.length,
    dispatched,
    cost_usd: cost,
    decisions,
    findings,
    open_checkpoint: open === null ? null : openCheckpoint(open),
    calls: callViews(dispatches),
    recall: recalledPayloadOf(events),
  };
}
