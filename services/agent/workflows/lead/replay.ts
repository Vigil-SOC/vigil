import type { DispatchPayload } from "../../contracts/events.js";
import type { DecisionPayload } from "./workflow.js";

// What an investigate run decided, and the calls that followed each decision.
// No digest and no mismatch counts: those belong to the hunt replay.
export interface InvestigateReplay {
  run_id: string;
  run_kind: "investigate";
  decisions: InvestigateDecision[];
}

export interface InvestigateDecision {
  iteration: number;
  // The decision event's envelope timestamp.
  at: string;
  action: string;
  rationale: string;
  worker: string | null;
  cost_usd: number;
  // Wall time of the lead's model turn; omitted when the ledger did not record it.
  duration_ms?: number;
  calls: unknown[];
}

export function investigateReplay(
  runId: string,
  events: readonly { kind: string; ts: string; payload: unknown }[],
): InvestigateReplay {
  const decisions: InvestigateDecision[] = [];
  let open: InvestigateDecision | null = null;
  for (const event of events) {
    if (event.kind === "decision") {
      const payload = event.payload as DecisionPayload;
      open = {
        iteration: decisions.length + 1,
        at: event.ts,
        action: payload.action,
        rationale: payload.rationale,
        worker: payload.worker ?? null,
        cost_usd: 0,
        ...(typeof payload.duration_ms === "number" ? { duration_ms: payload.duration_ms } : {}),
        calls: [],
      };
      decisions.push(open);
      continue;
    }
    if (event.kind !== "dispatch" || open === null) continue;
    const payload = event.payload as DispatchPayload;
    if (typeof payload.cost_usd === "number") open.cost_usd += payload.cost_usd;
    if (Array.isArray(payload.calls)) open.calls.push(...payload.calls);
  }
  return { run_id: runId, run_kind: "investigate", decisions };
}
