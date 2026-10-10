import { describe, expect, it } from "vitest";
import { seedFrom } from "../../core/budget.js";
import { steer } from "../../workflows/hunt/inbox.js";
import { Journal } from "../../workflows/hunt/journal.js";
import { bookOf, finalized, newLedger, question, reopen } from "../support/hunt.js";

// coverage-is-satisfiable.test.ts went with the lead's decision schema: rule_evidence
// takes the rulings now, and its "neither" is pinned in hypothesis-loop.test.ts.

describe("ledger", () => {
  it("appends without rewriting, and reads back the same projection", async () => {
    const { ledger, state, queue, runId } = await newLedger();
    const before = (await Journal.open(state, queue, runId)).log.map((event) => [event.seq, event.kind]);
    question(ledger, "which host?");
    await ledger.flush();

    // Append-only: the prefix the earlier events occupied is untouched, and the
    // new event is appended past it rather than rewriting anything.
    const after = (await Journal.open(state, queue, runId)).log.map((event) => [event.seq, event.kind]);
    expect(after.slice(0, before.length)).toEqual(before);
    expect(after.length).toBe(before.length + 1);

    expect(ledger.projection.questions.size).toBe(1);
    expect((await Journal.open(state, queue, runId)).projection).toEqual(ledger.projection);
  });

  it("applies patches to the projection", async () => {
    const { ledger, hypothesisIds } = await newLedger();
    ledger.patch("hypothesis", hypothesisIds[0]!, { status: "parked" });
    expect(ledger.projection.hypotheses.get(hypothesisIds[0]!)!.status).toBe("parked");
  });

  // The budget counter is the sum of the spend events, the same sum seedFrom() hands
  // the harness pool -- not a figure patched in when an iteration finished. So the
  // stream's writes, which never pass through the journal, show after a refresh.
  it("folds hunt.cost_usd from the spend events, as the harness pool does", async () => {
    const { ledger, state, runId, spend } = await newLedger();
    await spend(0.25, "lead");
    await spend(0.5, "threat_hunter");
    expect(ledger.projection.hunt.cost_usd).toBe(0);

    await ledger.refresh();
    expect(ledger.projection.hunt.cost_usd).toBeCloseTo(0.75, 10);
    expect(ledger.projection.hunt.cost_usd).toBe(seedFrom(await state.read(runId)).spent.cost_usd);
  });
});

// The controller's iteration machinery -- decision validation, the bounded re-prompt,
// worker fan-out -- went with it; a kernel hunt's turn is tests/hunt/kernel-hunt.test.ts.
describe("the book", () => {
  it("concludes once nothing is left active, and the report carries the spend", async () => {
    const started = await newLedger();
    started.ledger.patch("hypothesis", started.hypothesisIds[0]!, { status: "parked" });
    await started.spend(0.25, "lead");
    // What withBook does each call: the stored ledger, the stream's spend included.
    const ledger = await reopen(started);

    expect(bookOf(ledger).conclude("nothing further to run").concluded).toBe(true);
    expect(ledger.projection.hunt.status).toBe("terminal");
    expect(ledger.projection.hunt.outcome).toBe("completed");
    // The report is frozen on the same call, so it must already carry that spend.
    expect(finalized(ledger)[0]!.cost_usd).toBe(0.25);
  });

  it("coerces unresolved hypotheses to inconclusive, never disproven", async () => {
    const { ledger, queue, runId } = await newLedger({ hypotheses: ["h one", "h two"] });
    await steer(queue, runId, "abort", "operator halted the hunt");
    expect(await bookOf(ledger).applyDirectives()).toBe(true);

    const statuses = [...ledger.projection.hypotheses.values()].map((h) => h.status);
    expect(statuses).toEqual(["inconclusive", "inconclusive"]);
  });

  it("never downgrades an outcome already on the record", async () => {
    const { ledger } = await newLedger();
    const book = bookOf(ledger);
    book.terminate("aborted");
    book.terminate("completed");
    expect(ledger.projection.hunt.outcome).toBe("aborted");
  });

  it("parks at the budget checkpoint rather than ending the hunt itself", async () => {
    const { ledger } = await newLedger({ budgets: { max_iterations: 1, max_calls: 12, max_cost_usd: 10, max_wall_ms: 1_800_000, max_park_ms: 604_800_000 } });
    const book = bookOf(ledger);
    book.advanceTo(1);

    // Running out of money is a question for an operator, not a verdict.
    expect(book.atCeiling("the hunt used its turns")).toBe("parked");
    expect(ledger.projection.hunt.status).toBe("parked");
    expect(ledger.projection.hunt.outcome).toBeNull();
    // Names the arm that bound, not just "budget": a run stopped at 3 of 3 turns
    // while $0.11 of $14 was spent reads as a broken ceiling unless the sentence
    // says which of the two ran out.
    expect(ledger.projection.hunt.parked_reason).toMatch(/ran out of turns: iteration 1 of 1/);
    expect(ledger.projection.hunt.parked_reason).toMatch(/having spent \$/);
  });
});
