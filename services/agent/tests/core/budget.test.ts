import { describe, expect, it } from "vitest";
import { addTokens, budgetOf, FRESH, unmeteredQuota } from "../../core/budget.js";
import { UNPRICED_TOLERANCE, ZERO_TOKENS, type Quota, type SpendPayload, type TokenCounts } from "../../contracts/budget.js";
import { noPrices } from "../../core/prices.js";

function tokens(counts: Partial<TokenCounts>): TokenCounts {
  return { input: 0, output: 0, cache_read: 0, cache_write: 0, ...counts };
}

function reporting(used_usd: number, limit_usd: number): Quota {
  return { spent: async () => ({ used_usd, limit_usd }) };
}

function spend(counts: Partial<TokenCounts>, cost_usd: number | null = null): SpendPayload {
  return { model_id: "openai/gpt-4o", provider_type: "openai", role: "lead", tokens: tokens(counts), cost_usd, pricing_source: null, rates: null, fetched_at: null };
}

function pool(quota: Quota, max_calls = 100, max_cost_usd = 25) {
  return budgetOf({ max_calls, max_cost_usd, max_wall_ms: 600_000, max_park_ms: 604_800_000 }, quota);
}

describe("calls are the harness's to count", () => {
  it("advances only on beginCall", async () => {
    const budget = pool(unmeteredQuota);
    expect(budget.spent.calls).toBe(0);
    await budget.beginCall();
    await budget.beginCall();
    expect(budget.spent.calls).toBe(2);
  });

  it("refuses once the cap is reached, without consuming another", async () => {
    const budget = pool(unmeteredQuota, 1);
    expect(await budget.beginCall()).toBeNull();
    expect(await budget.beginCall()).toEqual({ reason: "calls_exhausted", used: 1, limit: 1 });
    expect(budget.spent.calls).toBe(1);
  });
});

describe("dollars are the gateway's to enforce", () => {
  it("parks before paying for an iteration the gateway has no room for", async () => {
    const budget = pool(reporting(25, 25));
    expect(await budget.beginCall()).toEqual({ reason: "cost_exhausted", used_usd: 25, limit_usd: 25 });
    expect(budget.spent.calls).toBe(0);
  });

  it("takes the lower of the run's ceiling and the gateway's", async () => {
    const budget = pool(reporting(11, 500), 100, 10);
    expect(await budget.beginCall()).toEqual({ reason: "cost_exhausted", used_usd: 11, limit_usd: 10 });
  });

  it("reports the gateway's number as spend rather than a local sum", async () => {
    const budget = pool(reporting(7.5, 25));
    await budget.beginCall();
    budget.record(spend({ input: 100 }, 999));
    expect(budget.spent.cost_usd).toBe(7.5 + 999);
    await budget.beginCall();
    expect(budget.spent.cost_usd).toBe(7.5);
  });

  // A gateway blip must not stop a run: the gateway still caps on its own side,
  // so an unreadable quota costs the pre-flight courtesy and nothing else.
  it("proceeds when the quota cannot be read", async () => {
    const budget = pool({ spent: async () => null });
    expect(await budget.beginCall()).toBeNull();
  });
});

describe("tokens are journaled exactly", () => {
  it("accumulates every counter", () => {
    const budget = pool(unmeteredQuota);
    budget.record(spend({ input: 10, output: 2, cache_read: 3, cache_write: 4 }));
    budget.record(spend({ input: 5, output: 1 }));
    expect(budget.spent.tokens).toEqual({ input: 15, output: 3, cache_read: 3, cache_write: 4 });
  });

  it("journals tokens even when the gateway reported no cost", () => {
    const budget = pool(unmeteredQuota);
    budget.record(spend({ input: 42 }, null));
    expect(budget.spent.tokens.input).toBe(42);
    expect(budget.spent.cost_usd).toBe(0);
  });

  it("addTokens sums each counter independently", () => {
    expect(addTokens(tokens({ input: 1, cache_read: 2 }), tokens({ input: 3, cache_write: 4 }))).toEqual({
      input: 4,
      output: 0,
      cache_read: 2,
      cache_write: 4,
    });
  });
});

describe("the wall clock is a ceiling of its own", () => {
  function clocked(max_wall_ms: number) {
    let at = 0;
    const budget = budgetOf({ max_calls: 100, max_cost_usd: 25, max_wall_ms, max_park_ms: 604_800_000 }, unmeteredQuota, () => at);
    return { budget, tick: (ms: number) => (at += ms) };
  }

  it("lets a call through inside the window", async () => {
    const { budget, tick } = clocked(1_000);
    tick(400);
    expect(await budget.beginCall()).toBeNull();
  });

  it("refuses once the window has passed, with its own reason", async () => {
    const { budget, tick } = clocked(1_000);
    tick(1_200);
    expect(await budget.beginCall()).toEqual({ reason: "wall_exhausted", used_ms: 1_200, limit_ms: 1_000 });
  });

  it("does not spend the call it refused", async () => {
    const { budget, tick } = clocked(1_000);
    await budget.beginCall();
    tick(5_000);
    await budget.beginCall();
    expect(budget.spent.calls).toBe(1);
  });

  it("refuses on wall before asking the gateway what was spent", async () => {
    let asked = 0;
    let at = 0;
    const quota = { spent: async () => { asked += 1; return null; } };
    const budget = budgetOf({ max_calls: 100, max_cost_usd: 25, max_wall_ms: 10, max_park_ms: 604_800_000 }, quota, () => at);
    at = 50;
    await budget.beginCall();
    expect(asked).toBe(0);
  });
});

// What made max_cost_usd mean something: its one reader compared a total nothing
// added to, so the ceiling refused nothing in any deployment.
describe("a call is priced from the backend's rates", () => {
  const RATES = { input: 3e-6, output: 15e-6, cache_read: 3e-7, cache_write: 3.75e-6, source: "exact", fetched_at: "2026-09-01T00:00:00+00:00" };

  function priced(rates = RATES, max_cost_usd = 25) {
    return budgetOf(
      { max_calls: 100, max_cost_usd, max_wall_ms: 600_000, max_park_ms: 604_800_000 },
      unmeteredQuota,
      Date.now,
      undefined,
      async () => rates,
    );
  }

  it("multiplies every token class by its own rate", async () => {
    const at = await priced().priceOf("m", "anthropic", tokens({ input: 1000, output: 100, cache_read: 10_000, cache_write: 200 }));

    // Cache is not input: read at a tenth, written at a quarter more, which is the
    // difference between billing a cached prompt right and billing it tenfold.
    expect(at.cost_usd).toBeCloseTo(0.003 + 0.0015 + 0.003 + 0.00075, 10);
    expect(at.source).toBe("exact");
    expect(at.fetched_at).toBe(RATES.fetched_at);
    expect(at.rates).toEqual({ input: RATES.input, output: RATES.output, cache_read: RATES.cache_read, cache_write: RATES.cache_write });
  });

  it("keeps unknown with null rates when the catalog prices nothing", async () => {
    const budget = budgetOf(
      { max_calls: 100, max_cost_usd: 25, max_wall_ms: 600_000, max_park_ms: 604_800_000 },
      unmeteredQuota,
      Date.now,
      undefined,
      async () => ({ input: null, output: null, cache_read: null, cache_write: null, source: "unknown", fetched_at: null }),
    );

    expect(await budget.priceOf("m", "anthropic", tokens({ input: 1000 }))).toEqual({
      cost_usd: null,
      source: "unknown",
      rates: null,
      fetched_at: null,
    });
  });

  // A $0.00 nobody could price and a $0.00 from a real entry are the same number
  // and nothing alike, so an unreachable pricer says null rather than free.
  it("says null rather than zero when nothing answered", async () => {
    const budget = budgetOf(
      { max_calls: 100, max_cost_usd: 25, max_wall_ms: 600_000, max_park_ms: 604_800_000 },
      unmeteredQuota,
      Date.now,
      undefined,
      async () => null,
    );

    expect(await budget.priceOf("m", "anthropic", tokens({ input: 1000 }))).toEqual({ cost_usd: null, source: null, rates: null, fetched_at: null });
  });

  // The ceiling with no gateway behind it, which is every deployment: unmeteredQuota
  // is what harness.ts wires, so the local total is the only thing holding the line.
  it("refuses a further call once the priced total passes the ceiling", async () => {
    const budget = priced(RATES, 0.01);
    expect(await budget.beginCall()).toBeNull();

    budget.record(spend({ input: 10_000 }, (await budget.priceOf("m", "anthropic", tokens({ input: 10_000 }))).cost_usd));

    expect(budget.spent.cost_usd).toBeCloseTo(0.03, 10);
    expect(await budget.beginCall()).toEqual({ reason: "cost_exhausted", used_usd: budget.spent.cost_usd, limit_usd: 0.01 });
  });
});

// Every deployment wires unmeteredQuota, so cost is the local total -- and it grows
// only when something priced the call. Unreachable pricing refused nothing at all.
describe("a dollar ceiling with nothing pricing the calls", () => {
  const limits = { max_calls: 100, max_cost_usd: 5, max_wall_ms: 600_000, max_park_ms: 604_800_000 };
  const unpriced = { model_id: "m", provider_type: "p", role: "lead", tokens: ZERO_TOKENS, cost_usd: null, pricing_source: null, rates: null, fetched_at: null };

  // A source that is wired and answering null, which is pricing failing rather
  // than pricing nobody asked for.
  const failing = async () => null;

  it("stops the run rather than spending on uncounted", async () => {
    const pool = budgetOf(limits, unmeteredQuota, Date.now, FRESH, failing);
    for (let call = 0; call < UNPRICED_TOLERANCE; call += 1) {
      expect(await pool.beginCall()).toBeNull();
      pool.record(unpriced);
    }
    expect(await pool.beginCall()).toEqual({ reason: "unpriced", calls: UNPRICED_TOLERANCE });
  });

  // A blip is not an outage: the price memo already rides one out, and refusing
  // on the first would park runs the catalog would have answered a second later.
  it("rides out fewer unpriced calls than the tolerance", async () => {
    const pool = budgetOf(limits, unmeteredQuota, Date.now, FRESH, failing);
    pool.record(unpriced);
    expect(await pool.beginCall()).toBeNull();
  });

  // Without a dollar ceiling there is nothing to fail to measure, so a run priced
  // by nobody is exactly as legitimate as it was before.
  it("says nothing when the run declares no cost ceiling", async () => {
    const pool = budgetOf({ ...limits, max_cost_usd: Infinity }, unmeteredQuota, Date.now, FRESH, failing);
    for (let call = 0; call < UNPRICED_TOLERANCE + 2; call += 1) pool.record(unpriced);
    expect(await pool.beginCall()).toBeNull();
  });

  // noPrices is the deliberate "nobody to ask" -- a deployment running without the
  // backend reachable, which core/prices.ts supports on purpose. It is not the dark.
  it("says nothing when the deployment asked for no pricing at all", async () => {
    const pool = budgetOf(limits, unmeteredQuota, Date.now, FRESH, noPrices);
    for (let call = 0; call < UNPRICED_TOLERANCE + 2; call += 1) pool.record(unpriced);
    expect(await pool.beginCall()).toBeNull();
  });
});
