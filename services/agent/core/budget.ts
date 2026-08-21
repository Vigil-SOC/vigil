import {
  UNPRICED_TOLERANCE,
  ZERO_TOKENS,
  type Budget,
  type BudgetLimits,
  type Priced,
  type Quota,
  type Refusal,
  type Spend,
  type SpendPayload,
  type TokenCounts,
} from "../contracts/budget.js";
import { costOf, noPrices, type Prices } from "./prices.js";

export function addTokens(left: TokenCounts, right: TokenCounts): TokenCounts {
  return {
    input: left.input + right.input,
    output: left.output + right.output,
    cache_read: left.cache_read + right.cache_read,
    cache_write: left.cache_write + right.cache_write,
  };
}

// Nothing to ask, so cost is held against the pool's own total rather than the
// gateway's. For tests and for any deployment running without a gateway key.
export const unmeteredQuota: Quota = { spent: async () => null };

// What the run has spent, off its own ledger. One spend event is one model call,
// which is what beginCall counts, so a resumed run continues its allowance.
export function seedFrom(events: readonly SpentEvent[], now = Date.now()): Seed {
  let spent: Spend = { calls: 0, cost_usd: 0, tokens: ZERO_TOKENS };
  let opened = 0;

  for (const event of events) {
    if (event.kind === "run") opened = Date.parse(event.ts);
    if (event.kind !== "spend") continue;
    const payload = event.payload as SpendPayload;
    spent = {
      calls: spent.calls + 1,
      cost_usd: spent.cost_usd + (payload.cost_usd ?? 0),
      tokens: addTokens(spent.tokens, payload.tokens),
    };
  }
  return { spent, started: opened === 0 ? 0 : opened + waitedMs(events, now) };
}

interface Wait {
  from: number;
  to: number;
}

// How long the run sat on an unanswered checkpoint, so max_wall_ms measures work
// rather than patience. Crash time still counts: only a checkpoint excuses the clock.
function waitedMs(events: readonly SpentEvent[], now: number): number {
  const answeredAt = new Map<string, number>();
  for (const event of events) {
    if (event.kind !== "resolution") continue;
    const { checkpoint_id: id } = event.payload as { checkpoint_id?: unknown };
    const at = Date.parse(event.ts);
    if (typeof id === "string" && Number.isFinite(at) && !answeredAt.has(id)) answeredAt.set(id, at);
  }

  // An unanswered checkpoint is still waiting, so its wait runs to now: a resume
  // must not bill the wait it is recovering from.
  const waits: Wait[] = [];
  for (const event of events) {
    if (event.kind !== "checkpoint") continue;
    const { checkpoint_id: id } = event.payload as { checkpoint_id?: unknown };
    const from = Date.parse(event.ts);
    if (typeof id !== "string" || !Number.isFinite(from)) continue;
    const to = answeredAt.get(id) ?? now;
    if (to > from) waits.push({ from, to });
  }

  // Merged rather than summed: two checkpoints open at once waited the same
  // seconds, and counting them twice would hand the run more wall time than it took.
  waits.sort((left, right) => left.from - right.from);
  let total = 0;
  let open: Wait | null = null;
  for (const wait of waits) {
    if (open !== null && wait.from <= open.to) {
      open.to = Math.max(open.to, wait.to);
      continue;
    }
    if (open !== null) total += open.to - open.from;
    open = { ...wait };
  }
  return open === null ? total : total + (open.to - open.from);
}

// Structural rather than AgentEvent<Kinds>: the fold reads two kinds and a
// timestamp, and never needs to know whose ledger it is walking.
export interface SpentEvent {
  kind: string;
  ts: string;
  payload: unknown;
}

// What a run has spent and where its wall clock reads from, folded off the ledger.
// started is the run event pushed forward by the time it sat parked, not when it opened.
export interface Seed {
  spent: Spend;
  started: number;
}

export const FRESH: Seed = { spent: { calls: 0, cost_usd: 0, tokens: ZERO_TOKENS }, started: 0 };

// One pool per run. Calls and elapsed time are counted here because no gateway knows
// either; dollars come from the gateway when it answers, the local total when not.
export function budgetOf(
  limits: BudgetLimits,
  quota: Quota,
  now = Date.now,
  seed: Seed = FRESH,
  prices: Prices = noPrices,
): Budget {
  return new Pool(limits, quota, now, seed, prices);
}

class Pool implements Budget {
  private calls: number;
  private cost: number;
  private tokens: TokenCounts;
  private unpriced = 0;
  private readonly started: number;
  // Whether anything was meant to price these calls. noPrices is the deliberate
  // "nobody to ask", and a run that asked for no pricing is not a run in the dark.
  private readonly priced: boolean;

  constructor(
    public limits: BudgetLimits,
    private readonly quota: Quota,
    private readonly now: () => number,
    seed: Seed,
    private readonly prices: Prices,
  ) {
    this.calls = seed.spent.calls;
    this.cost = seed.spent.cost_usd;
    this.tokens = seed.spent.tokens;
    // The run's own start, not this process's: a resume that restarted the clock
    // would hand a killed run a fresh wall-time allowance on every attempt.
    this.started = seed.started === 0 ? now() : seed.started;
    this.priced = prices !== noPrices;
  }

  raise(limits: Partial<BudgetLimits>): void {
    // A limit that is not a finite number is not a limit: every comparison against
    // NaN is false, so storing one would delete the ceiling instead of raising it.
    const asked = (value: number | undefined, held: number): number =>
      typeof value === "number" && Number.isFinite(value) ? Math.max(held, value) : held;
    this.limits = {
      max_calls: asked(limits.max_calls, this.limits.max_calls),
      max_cost_usd: asked(limits.max_cost_usd, this.limits.max_cost_usd),
      max_wall_ms: asked(limits.max_wall_ms, this.limits.max_wall_ms),
      max_park_ms: asked(limits.max_park_ms, this.limits.max_park_ms),
    };
  }

  get spent(): Spend {
    return { calls: this.calls, cost_usd: this.cost, tokens: { ...this.tokens } };
  }

  // Wall before cost: a run already over time should not spend a gateway round
  // trip finding out it is also over budget.
  async beginCall(): Promise<Refusal | null> {
    const limit = this.limits.max_calls;
    if (this.calls >= limit) return { reason: "calls_exhausted", used: this.calls, limit };

    const used_ms = this.now() - this.started;
    if (used_ms >= this.limits.max_wall_ms) {
      return { reason: "wall_exhausted", used_ms, limit_ms: this.limits.max_wall_ms };
    }

    const refusal = await this.overspent();
    if (refusal !== null) return refusal;

    this.calls += 1;
    return null;
  }

  record(payload: SpendPayload): void {
    this.tokens = addTokens(this.tokens, payload.tokens);
    if (payload.cost_usd === null) this.unpriced += 1;
    else this.cost += payload.cost_usd;
  }

  // The backend catalog's rates, never a second copy of it. Null rather than zero
  // when nothing answered: an unpriced call must not read as a free one.
  async priceOf(modelId: string, providerType: string, tokens: TokenCounts): Promise<Priced> {
    const rates = await this.prices(modelId, providerType);
    return rates === null ? { cost_usd: null, source: null } : { cost_usd: costOf(rates, tokens), source: rates.source };
  }

  // An unreadable quota is not a refusal, nor a licence: the local total is held
  // against max_cost_usd either way. Every deployment takes this arm today.
  private async overspent(): Promise<Refusal | null> {
    const reported = await this.quota.spent();
    if (reported === null) {
      const limit_usd = this.limits.max_cost_usd;
      // A ceiling nothing can measure is not one: cost never grows, so the comparison
      // below refuses nothing. Only where pricing was wired and is failing.
      if (this.priced && Number.isFinite(limit_usd) && this.unpriced >= UNPRICED_TOLERANCE) {
        return { reason: "unpriced", calls: this.unpriced };
      }
      if (this.cost < limit_usd) return null;
      return { reason: "cost_exhausted", used_usd: this.cost, limit_usd };
    }

    // The gateway prices, so where it and the fold disagree it is the authority.
    this.cost = reported.used_usd;
    const limit_usd = Math.min(this.limits.max_cost_usd, reported.limit_usd);
    if (reported.used_usd < limit_usd) return null;
    return { reason: "cost_exhausted", used_usd: reported.used_usd, limit_usd };
  }
}
