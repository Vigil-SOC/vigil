import type { UnboundPayload } from "../../contracts/events.js";

// What the lead loop journaled, before investigate ran on the plugin kernel. Kept
// so a ledger it wrote still projects and replays; nothing writes these now.

export interface DecisionPayload {
  action: string;
  rationale: string;
  worker: string | null;
  // Wall time of the lead's model turn; absent on ledgers written before it was kept.
  duration_ms?: number;
}

export interface FindingPayload {
  agent_id: string;
  answer: unknown;
}

export type LeadKinds = { decision: DecisionPayload; finding: FindingPayload; unbound: UnboundPayload };
