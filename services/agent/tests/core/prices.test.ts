import { describe, expect, it } from "vitest";
import { costOf, httpPrices, ratesOf } from "../../core/prices.js";

const RATES = { input: 3e-6, output: 1.5e-5, cache_read: 3e-7, cache_write: 3.75e-6 };

function answering(body: unknown, seen: string[] = []): typeof globalThis.fetch {
  return (async (url: string) => {
    seen.push(String(url));
    return { ok: true, json: async () => body } as Response;
  }) as unknown as typeof globalThis.fetch;
}

// The catalog answers four zeros for a model nothing matched, which parse as
// perfectly good rates. Reading them as a price is how a run spends unmetered.
describe("what the catalog could not price", () => {
  it("keeps unknown, with null rates rather than the zeros", () => {
    expect(ratesOf({ input: 0, output: 0, cache_read: 0, cache_write: 0, source: "unknown", fetched_at: "2026-01-01T00:00:00+00:00" })).toEqual({
      input: null,
      output: null,
      cache_read: null,
      cache_write: null,
      source: "unknown",
      fetched_at: null,
    });
  });

  it("still prices a model that genuinely costs nothing", () => {
    const rates = ratesOf({ input: 0, output: 0, cache_read: 0, cache_write: 0, source: "zero", fetched_at: "2026-01-01T00:00:00+00:00" });
    expect(rates?.source).toBe("zero");
    expect(rates?.fetched_at).toBe("2026-01-01T00:00:00+00:00");
    if (rates === null || rates.input === null) throw new Error("a zero-priced model is a price");
    expect(costOf(rates, { input: 1000, output: 1000, cache_read: 0, cache_write: 0 })).toBe(0);
  });

  it("prices nothing when a rate is missing rather than reading it as free", () => {
    expect(ratesOf({ input: 3e-6, output: 1.5e-5, cache_read: 3e-7, source: "exact" })).toBeNull();
  });
});

describe("asking the catalog", () => {
  it("keeps a price it was given and asks once", async () => {
    const seen: string[] = [];
    const fetched_at = "2026-01-02T03:04:05+00:00";
    const prices = httpPrices({ url: "http://backend/internal/pricing", token: "t", ttlMs: 300_000, fetch: answering({ ...RATES, source: "exact", fetched_at }, seen) });
    const first = await prices("claude-sonnet-4-6", "bifrost");
    expect(first?.fetched_at).toBe(fetched_at);
    expect(await prices("claude-sonnet-4-6", "bifrost")).toEqual(first);
    expect(seen).toHaveLength(1);
  });

  // The backend re-reads the gateway every refresh interval; a memo outliving that
  // would keep charging a repriced model at its old rate for the life of the process.
  it("asks again once the memo is older than the backend's refresh interval", async () => {
    const seen: string[] = [];
    let clock = 0;
    const prices = httpPrices({
      url: "http://backend/internal/pricing",
      token: "t",
      ttlMs: 300_000,
      now: () => clock,
      fetch: answering({ ...RATES, source: "exact", fetched_at: "2026-01-02T03:04:05+00:00" }, seen),
    });
    await prices("claude-sonnet-4-6", "bifrost");
    clock = 299_999;
    await prices("claude-sonnet-4-6", "bifrost");
    expect(seen).toHaveLength(1);
    clock = 300_000;
    await prices("claude-sonnet-4-6", "bifrost");
    expect(seen).toHaveLength(2);
  });

  // Kept out of the memo deliberately: the run refuses after UNPRICED_TOLERANCE,
  // and a cached null would outlive a catalog that has since been given the model.
  it("asks again after an answer that priced nothing", async () => {
    const seen: string[] = [];
    const prices = httpPrices({ url: "http://backend/internal/pricing", token: "t", ttlMs: 300_000, fetch: answering({ ...RATES, input: 0, output: 0, cache_read: 0, cache_write: 0, source: "unknown" }, seen) });
    const unpriced = { input: null, output: null, cache_read: null, cache_write: null, source: "unknown", fetched_at: null };
    expect(await prices("nobody-knows", "bifrost")).toEqual(unpriced);
    expect(await prices("nobody-knows", "bifrost")).toEqual(unpriced);
    expect(seen).toHaveLength(2);
  });
});
