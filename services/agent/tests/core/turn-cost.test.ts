import { describe, expect, it } from "vitest";
import { drain, streamTurn } from "../../core/stream.js";
import type { Harness, TurnConfig } from "../../core/loop.js";
import { budgetOf, unmeteredQuota } from "../../core/budget.js";
import { registryOf } from "../../core/registry.js";
import { InProcessState } from "../../core/state.js";
import { nullMemory } from "../../core/memory.js";
import { localDispatch } from "../../core/dispatch.js";
import { scriptedProvider, type ScriptedTurn } from "../support/scripted-provider.js";

const RUN = "5a2c2d3e-0000-4000-8000-000000000593";
const SCHEMA = { type: "object", required: ["verb"], properties: { verb: { type: "string", enum: ["TALLY"] } } };
const ANSWER: ScriptedTurn = { emit: { verb: "TALLY" }, tokens: { input: 1_000, output: 1_000 } };

// Rates are per token, so a millionth either way makes one call cost $0.002.
const RATE = 1e-6;
const prices = async () => ({ input: RATE, output: RATE, cache_read: 0, cache_write: 0, source: "exact" });

const pool = () =>
  budgetOf(
    { max_calls: 10, max_cost_usd: 100, max_wall_ms: 600_000, max_park_ms: 604_800_000 },
    unmeteredQuota,
    Date.now,
    undefined,
    prices,
  );

// One pool, its own provider per turn. That is the shape the hunt dispatches in:
// several turns in flight at once, all billing the same ceiling.
function harnessOf(budget = pool()): Harness {
  return {
    provider: scriptedProvider([ANSWER]),
    registry: registryOf([], {}),
    dispatch: localDispatch,
    budget,
    memory: nullMemory,
    state: new InProcessState(),
  };
}

function config(role: string): TurnConfig {
  return {
    run_id: RUN,
    run_kind: "tally",
    role,
    system: "count things",
    task: "count to one",
    schema: SCHEMA,
    max_turns: 2,
    approvals: new Set(),
    verbs: ["TALLY"],
    result_cap: 4_000,
    recall_limit: 5,
  };
}

// Callers read the pool's running total before and after a turn to price it, which
// is only that turn's spend when nothing else is spending. Concurrent turns each
// billed the whole window: one hunt reported $0.82 against $0.50 of journaled spend.
describe("a turn reports what it spent, not what the pool moved", () => {
  it("prices one turn", async () => {
    const outcome = await drain(streamTurn(config("a"), harnessOf()));
    expect(outcome.cost_usd).toBeCloseTo(0.002, 6);
  });

  it("does not bill a concurrent turn's spend to its neighbours", async () => {
    const shared = pool();
    const outcomes = await Promise.all(
      ["a", "b", "c"].map((role) => drain(streamTurn(config(role), harnessOf(shared)))),
    );
    for (const outcome of outcomes) expect(outcome.cost_usd).toBeCloseTo(0.002, 6);
    // The sum is what the pool journaled, which is the whole point: the parts add up.
    const summed = outcomes.reduce((total, one) => total + one.cost_usd, 0);
    expect(summed).toBeCloseTo(shared.spent.cost_usd, 6);
  });
});
