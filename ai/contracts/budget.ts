// Contract 3 of 5 (issue #589). Consumed by WS-A (the seam), WS-D (#596, which
// must use it rather than keep parallel accounting) and WS-E (#593 pricing).

export interface TokenCounts {
  input: number;
  output: number;
  cache_read: number;
  cache_write: number;
}

export interface BudgetLimits {
  max_iterations: number;
  max_cost_usd: number;
}

export interface Spend {
  iterations: number;
  cost_usd: number;
  tokens: TokenCounts;
}

// One model call, journaled. Replaces the cost_usd field the prototype hung on
// decision and dispatch records, so the budget is a fold over one kind of event.
export interface SpendPayload {
  model_id: string;
  provider_type: string;
  role: string;
  tokens: TokenCounts;
  cost_usd: number;
  pricing_source: string;
  reservation_id: string | null;
}

// A value, never a throw: the exhaustiveness argument in ADR-0001 applies here
// or nowhere. unpriced_model fails closed so a missing rate cannot bill zero.
export type Refusal =
  | { reason: "iterations_exhausted"; used: number; limit: number }
  | { reason: "cost_exhausted"; used_usd: number; limit_usd: number }
  | { reason: "would_exceed"; estimate_usd: number; remaining_usd: number }
  | { reason: "unpriced_model"; model_id: string };

export interface Reservation {
  readonly id: string;
  readonly estimate_usd: number;
}

export type ReserveOutcome =
  | { ok: true; reservation: Reservation }
  | { ok: false; refusal: Refusal };

// Reserve/commit rather than check/spend: with dispatch.max_workers above one,
// concurrent independent checks would each pass and collectively overshoot.
export interface Budget {
  readonly limits: BudgetLimits;
  readonly spent: Spend;
  reserve(model_id: string, estimate_tokens: TokenCounts): ReserveOutcome;
  commit(reservation: Reservation, actual: SpendPayload): void;
  release(reservation: Reservation): void;
}

export const ZERO_TOKENS: TokenCounts = { input: 0, output: 0, cache_read: 0, cache_write: 0 };
