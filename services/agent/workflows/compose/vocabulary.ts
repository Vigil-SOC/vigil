import type { UnboundPayload } from "../../contracts/events.js";

// What a step answers with, and what the ledger holds for it. Compose owns both:
// the arch declares no roles, so there is no operator-authored schema to inherit.

// The class a parked step raises. Whoever answers it names this, so a resolution
// meant for one kind of pause cannot release a different one.
export const PHASE_APPROVAL = "phase_approval";

// Deliberately thin. A step is run by whichever agent the playbook named, and a
// schema that assumed more would constrain agents this workflow has never seen.
export const PHASE_SCHEMA: Record<string, unknown> = {
  type: "object",
  additionalProperties: false,
  required: ["summary", "handoff"],
  properties: {
    summary: { type: "string" },
    // Written for the next step rather than for a reader: the steps after this
    // one are given it, and a summary addressed to nobody gets ignored by both.
    handoff: { type: "string" },
    details: { type: "object" },
    citations: { type: "array", items: { type: "string" } },
  },
};

export interface PhaseAnswer {
  summary: string;
  handoff: string;
  details?: Record<string, unknown>;
  citations?: string[];
}

// One completed step. answer is kept whole so a later fold can render the report
// without the workflow having decided in advance what a reader would want.
export interface PhasePayload {
  phase_id: string;
  agent: string;
  name: string;
  answer: PhaseAnswer;
}

export type ComposeKinds = { phase: PhasePayload; unbound: UnboundPayload };
