// One of the four Phase-0 contracts. Consumed by the harness seam and by any
// workflow, which must use it rather than keep parallel accounting.

export interface TokenCounts {
  input: number;
  output: number;
  cache_read: number;
  cache_write: number;
}

// Calls, not decisions: one decision costs several model calls. max_park_ms is the
// sweeper's rather than the pool's, but it is a ceiling on the run like the rest.
export interface BudgetLimits {
  max_calls: number;
  max_cost_usd: number;
  max_wall_ms: number;
  max_park_ms: number;
}

// Seven days to answer a checkpoint: abandoning is irreversible, but an answer
// three weeks late resumes a run whose transcript describes a world that has moved.
export const DEFAULT_PARK_MS = 604_800_000;

export interface Spend {
  calls: number;
  cost_usd: number;
  tokens: TokenCounts;
}

// One model call, journaled. Tokens are the provider's and exact; cost is the
// catalog's rates over them, null when nothing could price it.
export interface SpendPayload {
  model_id: string;
  provider_type: string;
  role: string;
  tokens: TokenCounts;
  cost_usd: number | null;
  // How the rates resolved, or null when nothing priced it: a $0.00 from a catalog
  // entry and a $0.00 nobody could price are the same number and nothing alike.
  pricing_source: string | null;
}

// A value, never a throw: the exhaustiveness argument applies here or nowhere.
// unpriced is a ceiling that cannot be measured rather than one that was reached.
export type Refusal =
  | { reason: "calls_exhausted"; used: number; limit: number }
  | { reason: "cost_exhausted"; used_usd: number; limit_usd: number }
  | { reason: "wall_exhausted"; used_ms: number; limit_ms: number }
  | { reason: "unpriced"; calls: number };

// How many calls may go unpriced before a run holding a dollar ceiling stops.
// Not one: a single blip is what the price memo already rides out.
export const UNPRICED_TOLERANCE = 3;

// What the gateway says has been spent against this run's key. Returning null
// means it could not be read, which is not a refusal: the gateway still caps.
export interface Quota {
  spent(): Promise<{ used_usd: number; limit_usd: number } | null>;
}

// Calls and wall are the harness's to count, dollars the gateway's to enforce.
// Checked once per call, so an exhausted run parks before paying for another.
export interface Budget {
  readonly limits: BudgetLimits;
  readonly spent: Spend;
  beginCall(): Promise<Refusal | null>;
  // Widens a ceiling an operator extended. Widen-only, so a resumed run cannot be
  // handed a smaller allowance than the one it was already refused under, and the
  // pool stays the single authority on what a run may still spend.
  raise(limits: Partial<BudgetLimits>): void;
  record(payload: SpendPayload): void;
  // What a call cost, for the ledger and for max_cost_usd. Here rather than on the
  // harness because this object already owns the ceiling and the running total.
  priceOf(modelId: string, providerType: string, tokens: TokenCounts): Promise<Priced>;
}

// Null cost means nothing could price it, which is not zero. The two are told apart
// everywhere downstream, so they are told apart here first.
export interface Priced {
  cost_usd: number | null;
  source: string | null;
}

export const ZERO_TOKENS: TokenCounts = { input: 0, output: 0, cache_read: 0, cache_write: 0 };
