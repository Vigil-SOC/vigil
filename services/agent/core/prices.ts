import type { TokenCounts } from "../contracts/budget.js";

// What a model costs per token, in USD. Four rates, not two: charging a cache read
// at the input rate over-bills it tenfold on Anthropic.
export interface Rates {
  input: number;
  output: number;
  cache_read: number;
  cache_write: number;
  // exact from the gateway's datasheet, zero for a model that costs nothing, unknown
  // when the gateway prices nothing -- otherwise two very different $0 calls look alike.
  source: string;
  // When the catalog read these rates off the gateway. The dollars multiply this
  // object, so the journal stamps this and not the time the memo was stored.
  fetched_at: string | null;
}

// The catalog's answer when it prices nothing. Rates are null, not the zeros the
// gateway sends for a gap: those must not be stored as a price.
export interface Unpriced {
  input: null;
  output: null;
  cache_read: null;
  cache_write: null;
  source: "unknown";
  fetched_at: null;
}

export type RateCard = Rates | Unpriced;

// Where a price comes from. A port because the catalog is the backend's: a second
// copy here would disagree with the dashboard after one repricing.
export type Prices = (modelId: string, providerType: string) => Promise<RateCard | null>;

// Nobody to ask, so nothing is priced and cost_usd stays null. The run still runs
// and its tokens are still journaled, which is what the ledger is for.
export const noPrices: Prices = async () => null;

export function costOf(rates: Rates, tokens: TokenCounts): number {
  return (
    tokens.input * rates.input +
    tokens.output * rates.output +
    tokens.cache_read * rates.cache_read +
    tokens.cache_write * rates.cache_write
  );
}

export interface PricesOptions {
  url: string;
  token: string;
  // How long a memoised rate is trusted: the backend's own refresh interval, since a
  // repricing on the gateway reaches the backend no faster than that.
  ttlMs: number;
  fetch?: typeof globalThis.fetch;
  now?: () => number;
}

// Memoised per model for ttlMs; only successes are kept, or one blip would disable
// pricing for the process. Never throws: an unpriced spend is a spend.
export function httpPrices(options: PricesOptions): Prices {
  const call = options.fetch ?? globalThis.fetch;
  const now = options.now ?? Date.now;
  const base = options.url.replace(/\/$/, "");
  const known = new Map<string, { rates: Rates; at: number }>();

  return async (modelId, providerType) => {
    const key = `${providerType}/${modelId}`;
    const held = known.get(key);
    if (held !== undefined && now() - held.at < options.ttlMs) return held.rates;

    const query = new URLSearchParams({ model_id: modelId, provider_type: providerType });
    try {
      const response = await call(`${base}/rates?${query.toString()}`, {
        headers: { "content-type": "application/json", authorization: `Bearer ${options.token}` },
      });
      if (!response.ok) return null;
      const rates = ratesOf(await response.json());
      // Unknown is an answer with nothing to multiply, and it is not memoised: the
      // catalog may learn the model inside this interval. A priced card is, and the
      // card carries the fetched_at the dollars were multiplied from.
      if (rates !== null && rates.input !== null) known.set(key, { rates, at: now() });
      return rates;
    } catch {
      return null;
    }
  };
}

// What the catalog says when the gateway prices nothing. Its rates are zeros, and they are a
// gap rather than a price -- the one distinction cost_usd null exists to carry.
const UNKNOWN = "unknown";

// A malformed answer prices nothing rather than pricing wrongly: a missing rate read
// as zero would silently under-bill every call for that model.
export function ratesOf(body: unknown): RateCard | null {
  const raw = body as Record<string, unknown> | null;
  if (raw === null || typeof raw !== "object") return null;
  // Before the rates, because they parse: four valid zeros under this source are
  // exactly the free-looking call the ledger must never record as free. `zero` is
  // the other thing entirely -- a self-hosted model that genuinely costs nothing.
  if (raw["source"] === UNKNOWN) {
    return { input: null, output: null, cache_read: null, cache_write: null, source: UNKNOWN, fetched_at: null };
  }

  const rate = (field: string): number | null => {
    const value = raw[field];
    return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
  };

  const input = rate("input");
  const output = rate("output");
  const cache_read = rate("cache_read");
  const cache_write = rate("cache_write");
  if (input === null || output === null || cache_read === null || cache_write === null) return null;

  const fetched = raw["fetched_at"];
  return {
    input,
    output,
    cache_read,
    cache_write,
    source: typeof raw["source"] === "string" ? raw["source"] : "unknown",
    fetched_at: typeof fetched === "string" ? fetched : null,
  };
}
