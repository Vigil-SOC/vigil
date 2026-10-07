import { describe, expect, it } from "vitest";
import { buildDigest } from "../../workflows/hunt/digest.js";
import { renderDigest } from "../../workflows/hunt/render.js";
import { newLedger } from "../support/hunt.js";

// "0 iteration(s), $9.79" stated both ceilings flatly and read as a contradiction:
// money to spend and no turn to spend it on, told to a lead that is being asked for a
// decision right now. Which ceiling binds is the fact worth having, and the slack on
// the other is worth saying too -- an operator reading the same line is who can lift it.
describe("the budget line says which ceiling binds", () => {
  async function rendered(iterations: number, cost_usd: number): Promise<string> {
    const { ledger } = await newLedger();
    const digest = buildDigest(ledger.projection, 1);
    return renderDigest({ ...digest, budget_remaining: { iterations, cost_usd } });
  }

  it("calls a spent turn count the last turn rather than none at all", async () => {
    const line = await rendered(0, 9.79);
    expect(line).toContain("This is the last turn");
    expect(line).toContain("turns are what bound this hunt, not money");
    // The slack is still stated: it is what an operator would extend.
    expect(line).toContain("$9.79");
  });

  it("says so when the money is what ran out", async () => {
    const line = await rendered(4, 0);
    expect(line).toContain("The allowance is spent");
    expect(line).toContain("4 turn(s) remain");
  });

  it("states both when both have room, and which one ends it", async () => {
    const line = await rendered(3, 4.5);
    expect(line).toContain("3 turn(s) after this one");
    expect(line).toContain("whichever runs out first");
  });
});
