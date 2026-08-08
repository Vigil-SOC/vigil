// Contract 5 of 5 (issue #589). Consumed by WS-E (#593) and WS-A, whose budget
// gate prices in-loop against it. Seeding and the deletion pass are #593.

export const PRICING_SOURCES = ["exact", "heuristic", "zero", "unknown"] as const;
export type PricingSource = (typeof PRICING_SOURCES)[number];

// Rates are USD per million tokens, here and everywhere. The Python registry
// stores per-million and hands out per-1k, which is the drift #593 was filed for.
export interface ModelRate {
  model_id: string;
  provider_type: string;
  input_per_mtok: number;
  output_per_mtok: number;
  cache_read_per_mtok: number;
  cache_write_per_mtok: number;
  pricing_source: PricingSource;
}

// Read once at startup and frozen: gating cannot afford a per-call round-trip.
// A miss returns undefined so the budget refuses, rather than pricing at zero.
export interface RateTable {
  lookup(model_id: string, provider_type: string): ModelRate | undefined;
  readonly size: number;
}

export function rateTableOf(rates: readonly ModelRate[]): RateTable {
  const byKey = new Map(rates.map((rate) => [`${rate.provider_type}/${rate.model_id}`, rate]));
  return { lookup: (model, provider) => byKey.get(`${provider}/${model}`), size: byKey.size };
}
