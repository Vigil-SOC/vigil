import { describe, expect, it } from "vitest";
import { parseGrant } from "../../workflows/hunt/inbox.js";
import { budgetOf, unmeteredQuota } from "../../core/budget.js";
import { DEFAULT_PARK_MS } from "../../contracts/budget.js";
import { DEFAULT_TERMINATION, terminationOf } from "../../workflows/hunt/config.js";

// The wall clock is the arm that actually stops a hunt, and it was the one arm an
// extension could not buy: a run stopped at 30 minutes with turns and dollars left
// had no answer but to start again.
describe("an extension can buy wall clock", () => {
  it("reads minutes out of the operator's own words", () => {
    expect(parseGrant("+30 minutes").wall_ms).toBe(1_800_000);
    expect(parseGrant("give it 5 more mins").wall_ms).toBe(300_000);
  });

  it("reads minutes beside iterations and dollars", () => {
    expect(parseGrant("3 more iterations, $2.50 and 20 minutes")).toEqual({
      iterations: 3,
      cost_usd: 2.5,
      wall_ms: 1_200_000,
    });
  });

  it("grants nothing from words that name no unit", () => {
    expect(parseGrant("keep going")).toEqual({ iterations: 0, cost_usd: 0, wall_ms: 0 });
  });
});

// A ledger opened before hard_max_wall_ms existed carries a termination block
// without that key. `?? DEFAULT_TERMINATION` only fires when the whole block is
// missing, so the new key read as undefined, Math.min gave NaN, and the run's
// wall ceiling was stored as null -- no ceiling at all, because used >= NaN is
// always false. The merge is key by key for exactly this reason.
describe("a threshold added after a hunt opened still resolves", () => {
  it("fills a key the journaled block predates", () => {
    const journaled = {
      priority_floor: 5,
      park_ttl_ms: 604_800_000,
      hard_max_iterations: 6,
      hard_max_calls: 960,
      hard_max_cost_usd: 16,
    };
    const resolved = { ...DEFAULT_TERMINATION, ...journaled };
    expect(Number.isFinite(resolved.hard_max_wall_ms)).toBe(true);
    // What the journaled block does say still wins over the default.
    expect(resolved.hard_max_iterations).toBe(6);
  });

  it("clamps to a number rather than NaN once the key resolves", () => {
    const partial: Partial<typeof DEFAULT_TERMINATION> = { hard_max_iterations: 6 };
    const { hard_max_wall_ms } = { ...DEFAULT_TERMINATION, ...partial };
    expect(Number.isFinite(Math.min(5_400_000, hard_max_wall_ms))).toBe(true);
  });
});

// A ceiling defined as twice the thing it caps is not a ceiling. A hunt started at
// 3 turns was capped at 6 for the rest of its life -- and because the ceiling is
// read from the spec journaled at open, extending it again re-clamped to the same
// 6, however much of its money was unspent.
describe("a small first ask does not lower the ceiling for good", () => {
  const asked = (max_iterations: number) =>
    terminationOf({
      budgets: { max_calls: 12, max_cost_usd: 5, max_wall_ms: 600_000, max_park_ms: DEFAULT_PARK_MS },
      thresholds: { max_iterations },
      dispatch: { max_workers: 4 },
      runtime: { max_turns: 8 },
    } as unknown as Parameters<typeof terminationOf>[0]);

  it("holds the deployment's ceiling when the run asked for less", () => {
    expect(asked(3).hard_max_iterations).toBe(DEFAULT_TERMINATION.hard_max_iterations);
    expect(asked(3).hard_max_iterations).toBeGreaterThan(6);
  });

  it("still gives headroom above a run that asked for more than the default", () => {
    const big = asked(20);
    expect(big.hard_max_iterations).toBe(40);
    expect(big.hard_max_iterations).toBeGreaterThan(DEFAULT_TERMINATION.hard_max_iterations);
  });

  it("leaves an explicitly configured ceiling alone", () => {
    const pinned = terminationOf({
      budgets: { max_calls: 12, max_cost_usd: 5, max_wall_ms: 600_000, max_park_ms: DEFAULT_PARK_MS },
      thresholds: { max_iterations: 3, hard_max_iterations: 4 },
      dispatch: { max_workers: 4 },
      runtime: { max_turns: 8 },
    } as unknown as Parameters<typeof terminationOf>[0]);
    expect(pinned.hard_max_iterations).toBe(4);
  });
});

describe("the pool's ceilings only ever widen", () => {
  const limits = { max_calls: 10, max_cost_usd: 5, max_wall_ms: 600_000, max_park_ms: DEFAULT_PARK_MS };

  it("takes a raise", () => {
    const pool = budgetOf(limits, unmeteredQuota);
    pool.raise({ max_wall_ms: 1_200_000 });
    expect(pool.limits.max_wall_ms).toBe(1_200_000);
  });

  it("refuses a narrowing, so a resume cannot be handed less than it was refused under", () => {
    const pool = budgetOf(limits, unmeteredQuota);
    pool.raise({ max_wall_ms: 60_000, max_cost_usd: 1 });
    expect(pool.limits.max_wall_ms).toBe(600_000);
    expect(pool.limits.max_cost_usd).toBe(5);
  });

  it("leaves an arm the raise does not name", () => {
    const pool = budgetOf(limits, unmeteredQuota);
    pool.raise({ max_wall_ms: 1_200_000 });
    expect(pool.limits.max_calls).toBe(10);
  });

  // A NaN ceiling is not a high ceiling, it is no ceiling: every comparison
  // against it is false, so beginCall would stop refusing anything.
  it("refuses a ceiling that is not a finite number", () => {
    const pool = budgetOf(limits, unmeteredQuota);
    pool.raise({ max_wall_ms: NaN, max_cost_usd: Number.POSITIVE_INFINITY });
    expect(pool.limits.max_wall_ms).toBe(600_000);
    expect(pool.limits.max_cost_usd).toBe(5);
  });
});
