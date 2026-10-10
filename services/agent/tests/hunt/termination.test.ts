import { afterEach, describe, expect, it, vi } from "vitest";
import { callsPerIteration, DEFAULT_BUDGETS, type Budgets } from "../../workflows/hunt/types.js";
import { scoredFrontier } from "../../workflows/hunt/frontier.js";
import {
  DEFAULT_TERMINATION,
  DEFAULT_VERDICTS,
  huntSpec,
  type Termination,
} from "../../workflows/hunt/config.js";
import { pendingCheckpoints } from "../../workflows/hunt/checkpoints.js";
import { steer } from "../../workflows/hunt/inbox.js";
import { Journal } from "../../workflows/hunt/journal.js";
import { boundBy, boundReason } from "../../workflows/hunt/opening.js";
import type { DirectiveQueue } from "../../workflows/hunt/ports.js";
import { buildReport, renderReport } from "../../workflows/hunt/report.js";
import { terminationVerdict } from "../../workflows/hunt/termination.js";
import {
  bookOf,
  finalized,
  gapLock,
  huntSpecFor,
  newLedger,
  question,
  reopen,
  resolve,
  type SpecOverrides,
} from "../support/hunt.js";

afterEach(() => {
  vi.restoreAllMocks();
});

const CAPPED: Budgets = { max_iterations: 1, max_calls: 12, max_cost_usd: 10, max_wall_ms: 1_800_000, max_park_ms: 604_800_000 };

// What the hunt plugin's before hook does at every boundary: operator input
// first, then answers that reached the ledger some other way.
async function boundary(ledger: Journal) {
  const book = bookOf(ledger);
  await book.applyDirectives();
  book.reconcile();
  await ledger.flush();
  return book;
}

// The hunt used its turns: what the finish hook does when the kernel caps it.
async function usedTurns(ledger: Journal, turns = ledger.projection.hunt.budgets.max_iterations) {
  const book = bookOf(ledger);
  book.advanceTo(turns);
  const ended = book.atCeiling("the hunt used its turns");
  await ledger.flush();
  return ended;
}

describe("the predicate, not the recommendation", () => {
  it("refuses to conclude while a hypothesis is active", async () => {
    const { ledger } = await newLedger();

    const result = bookOf(ledger).conclude("nothing further to run");

    expect(result.concluded).toBe(false);
    expect(result.said).toMatch(/refused: .* is still active/);
    expect(ledger.projection.hunt.status).toBe("active");
    expect(ledger.projection.hunt.outcome).toBeNull();
    // Re-asking the lead and the digest it read went with the controller: the
    // refusal is the tool's result, which the agent reads on its next turn.
  });

  it("refuses to conclude when a lead at or above the floor is still open", async () => {
    const { ledger, hypothesisIds } = await newLedger();
    resolve(ledger, hypothesisIds[0]!);
    question(ledger, "who else used this key?", { entity_key: "aws_key:AKIA1", spawned_iteration: 1 });

    const result = bookOf(ledger).conclude("nothing further to run");

    expect(ledger.projection.hunt.status).toBe("active");
    expect(result.said).toMatch(/who else used this key\?/);
    expect(result.said).toMatch(/priority floor of 5/);
  });

  it("completes and auto-parks the sub-floor leads as backlog", async () => {
    const { ledger, hypothesisIds } = await newLedger();
    resolve(ledger, hypothesisIds[0]!);
    // Already covered by a lead that was taken, so it is not novel: below the floor.
    question(ledger, "recheck 10.0.0.9", { entity_key: "ip:10.0.0.9", status: "closed" });
    const parked = question(ledger, "recheck 10.0.0.9 next quarter", { entity_key: "ip:10.0.0.9" });

    expect(bookOf(ledger).conclude("nothing further to run").concluded).toBe(true);

    expect(ledger.projection.hunt.outcome).toBe("completed");
    expect(ledger.projection.questions.get(parked)!.status).toBe("parked");
    expect(ledger.projection.questions.get(parked)!.closed_reason).toMatch(/below the priority floor/);

    // The backlog is a deliverable, so it has to survive into the report.
    expect(finalized(ledger)[0]!.backlog.map((entry) => entry.question)).toEqual([
      "recheck 10.0.0.9 next quarter",
    ]);
  });

  it("ends data_starved rather than completed when a hypothesis was gap-locked", async () => {
    const { ledger, hypothesisIds } = await newLedger();
    await gapLock(ledger, hypothesisIds[0]!);
    expect(ledger.projection.hypotheses.get(hypothesisIds[0]!)!.status).toBe("active");

    bookOf(ledger).conclude("nothing further to run");

    // A hunt that could not see is not a hunt that finished, and it keeps the status
    // its rulings earned rather than reading as if evidence had turned.
    expect(ledger.projection.hunt.outcome).toBe("data_starved");
    expect(ledger.projection.hypotheses.get(hypothesisIds[0]!)!.status).toBe("active");
    expect(ledger.projection.hypotheses.get(hypothesisIds[0]!)!.resolution_reason).toMatch(/could not finish: 3 sources unreachable/);
    expect(finalized(ledger)[0]!.gaps).toHaveLength(DEFAULT_VERDICTS.gap_lock_threshold);
  });

  it("reads gap-lock off the strength snapshot, not the resolution_reason prose", async () => {
    const { ledger, hypothesisIds } = await newLedger();
    // Says gap-locked, but the numbers say otherwise: the numbers win.
    ledger.patch("hypothesis", hypothesisIds[0]!, {
      status: "inconclusive",
      resolution_reason: "gap-locked: nothing could be seen",
      evidence_strength: {
        corroborating_sources: 1,
        contradicting_records: 0,
        open_gaps: 0,
        attacker_influenceable_only: false,
        survived_disconfirmation: false,
      },
    });

    expect(terminationVerdict(ledger.projection, 1, DEFAULT_TERMINATION, DEFAULT_VERDICTS).outcome).toBe("completed");
  });

  it("keeps completed when something was proven, however blind the rest of the hunt was", async () => {
    const { ledger, hypothesisIds } = await newLedger({ hypotheses: ["h one", "h two"] });
    await gapLock(ledger, hypothesisIds[0]!);
    ledger.patch("hypothesis", hypothesisIds[1]!, { status: "proven", resolution_reason: "survived" });

    expect(terminationVerdict(ledger.projection, 2, DEFAULT_TERMINATION, DEFAULT_VERDICTS).outcome).toBe("completed");
  });

  it("measures the floor against the same score the frontier is ranked by", async () => {
    const { ledger, hypothesisIds } = await newLedger();
    resolve(ledger, hypothesisIds[0]!);
    question(ledger, "fresh thread", { entity_key: "ip:10.0.0.7", spawned_iteration: 1 });

    const [top] = scoredFrontier(ledger.projection, 1);
    expect(top!.score).toBeGreaterThanOrEqual(DEFAULT_TERMINATION.priority_floor);

    expect(terminationVerdict(ledger.projection, 1, DEFAULT_TERMINATION, DEFAULT_VERDICTS).outcome).toBeNull();
    // Same frontier, a floor above every score: now nothing blocks.
    const cleared = terminationVerdict(
      ledger.projection,
      1,
      { ...DEFAULT_TERMINATION, priority_floor: 99 },
      DEFAULT_VERDICTS,
    );
    expect(cleared.outcome).toBe("completed");
  });
});

describe("the budget checkpoint", () => {
  async function parkedHunt(overrides: SpecOverrides = {}) {
    const started = await newLedger({ budgets: CAPPED, ...overrides });
    const ended = await usedTurns(started.ledger);
    return { ...started, ended };
  }

  it("parks instead of terminating, and says why while parked", async () => {
    const { ledger, ended } = await parkedHunt();

    expect(ended).toBe("parked");
    expect(ledger.projection.hunt.status).toBe("parked");
    expect(ledger.projection.hunt.outcome).toBeNull();
    expect(ledger.projection.hunt.parked_at).not.toBeNull();
    expect(ledger.projection.hunt.parked_reason).toMatch(/ran out of turns/);

    // A boundary with nothing answered leaves it where it is, and what the console
    // shows on the run row names what bound the hunt and the ways out.
    await boundary(ledger);
    expect(ledger.projection.hunt.status).toBe("parked");
    expect(bookOf(ledger).suspendedBecause()).toMatch(/ran out of turns.*Keep going.*conclude/s);
  });

  // A budget park is not an approval gate, so it raises no checkpoint: a checkpoint
  // event is the harness's own channel and Run.settled() reads an unresolved one as
  // "no model call may proceed". Raising one here parked the hunt in a way an
  // extension could not lift -- the budget went up, the hunt came back active, and
  // every call behind it was refused three times. The park says what it is through
  // the projection instead, which is what a reader was missing all along.
  it("parks without blocking the harness on an approval it never needed", async () => {
    const { ledger } = await parkedHunt();

    expect(pendingCheckpoints(ledger.projection)).toHaveLength(0);
    expect(ledger.projection.hunt.status).toBe("parked");
    expect(ledger.projection.hunt.parked_reason).toMatch(/ran out of turns|spent its allowance/);
  });

  // The round trip an operator actually makes: the hunt parks, they extend, it runs on.
  it("runs on when an extension answers the park", async () => {
    const { ledger, state, queue, runId } = await parkedHunt();
    await steer(queue, runId, "extend", "2 more iterations and $4");

    const resumed = await reopen({ ledger, state, queue, runId });
    await boundary(resumed);

    expect(resumed.projection.hunt.status).toBe("active");
    expect(resumed.projection.hunt.budgets.max_iterations).toBeGreaterThan(1);
  });

  // The other arm: an extension too small to buy a turn leaves the hunt parked rather
  // than resuming it into an immediate second park.
  it("stays parked when the extension bought no turn", async () => {
    const { ledger, state, queue, runId } = await parkedHunt();
    await steer(queue, runId, "extend", "nothing in particular");

    const resumed = await reopen({ ledger, state, queue, runId });
    await boundary(resumed);

    expect(resumed.projection.hunt.status).toBe("parked");
  });

  // The turn that crosses the cost ceiling is the one the harness refused, so a counter
  // patched at decision time under-reported it, and an extension could un-park too early.
  it("parks on cost at what was actually spent, and an extension that buys nothing keeps it parked", async () => {
    const budgets: Budgets = { ...CAPPED, max_iterations: 8, max_calls: 96, max_cost_usd: 1 };
    const started = await newLedger({ budgets });
    const { queue, runId, spend } = started;
    await spend(1.5, "hunt_agent");
    const spent = await reopen(started);
    expect(await usedTurns(spent, 1)).toBe("parked");

    expect(spent.projection.hunt.cost_usd).toBe(1.5);
    expect(spent.projection.hunt.parked_reason).toMatch(/spent its allowance: \$1\.5000 of \$1\.00/);

    // The overshoot is $0.50; $0.25 raises the ceiling to $1.25 and buys no turn.
    await steer(queue, runId, "extend", "", { grant: { iterations: 0, cost_usd: 0.25, wall_ms: 0 } });
    const short = await reopen(started, spent);
    await boundary(short);
    expect(short.projection.hunt.status).toBe("parked");
    expect(short.projection.hunt.budgets.max_cost_usd).toBe(1.25);

    await steer(queue, runId, "extend", "", { grant: { iterations: 0, cost_usd: 1, wall_ms: 0 } });
    const resumed = await reopen(started, short);
    await boundary(resumed);
    expect(resumed.projection.hunt.status).toBe("active");
    expect(resumed.projection.hunt.budgets.max_cost_usd).toBe(2.25);
  });

  // An extension that buys no room says so, rather than leaving a silent park.
  it("says so when an extension leaves no room", async () => {
    const budgets: Budgets = { ...CAPPED, max_iterations: 8, max_calls: 96, max_cost_usd: 1 };
    const started = await newLedger({ budgets });
    await started.spend(1.5, "hunt_agent");
    const spent = await reopen(started);
    await usedTurns(spent, 1);

    await steer(started.queue, started.runId, "extend", "", { grant: { iterations: 0, cost_usd: 0.25, wall_ms: 0 } });
    const short = await reopen(started, spent);
    await boundary(short);

    expect(short.projection.directives.map((directive) => directive.text).join(" ")).toMatch(/leaves no room .* stays parked/);
  });

  // The park the issue describes: the pool refused a call mid-turn, so only the spend
  // events say what it cost. The resume folds those, and extend() is measured against them.
  it("measures an extension against the spend of the turn that was refused", async () => {
    const budgets: Budgets = { ...CAPPED, max_iterations: 8, max_calls: 96, max_cost_usd: 1 };
    const started = await newLedger({ budgets });
    const { ledger, queue, runId, spend } = started;
    await spend(0.7, "hunt_agent");
    await spend(0.8, "critic");
    bookOf(ledger).parkOnRefusal("cost_exhausted");
    expect(ledger.projection.hunt.cost_usd).toBe(0);

    await steer(queue, runId, "extend", "", { grant: { iterations: 0, cost_usd: 0.25, wall_ms: 0 } });
    const resumed = await reopen(started);
    expect(resumed.projection.hunt.cost_usd).toBe(1.5);
    await boundary(resumed);
    expect(resumed.projection.hunt.status).toBe("parked");

    await steer(queue, runId, "extend", "", { grant: { iterations: 0, cost_usd: 1, wall_ms: 0 } });
    const again = await reopen(started, resumed);
    await boundary(again);
    expect(again.projection.hunt.status).toBe("active");
  });

  it("concludes rather than parking when the budget runs out on a finished hunt", async () => {
    const { ledger, hypothesisIds } = await newLedger({ budgets: CAPPED });
    resolve(ledger, hypothesisIds[0]!);

    const ended = await usedTurns(ledger);

    // Nothing was left to do, so there is nothing to ask an operator about, and
    // budget_terminated would say the hunt stopped short when it did not.
    expect(ended).toBe("concluded");
    expect(ledger.projection.hunt.status).toBe("terminal");
    expect(ledger.projection.hunt.outcome).toBe("completed");
    expect(ledger.projection.hunt.termination_reason).toMatch(/budget ran out/);
    expect(finalized(ledger)).toHaveLength(1);
  });

  it("takes the outcome from the predicate, not from having run out of money", async () => {
    const { ledger, hypothesisIds } = await newLedger({
      budgets: { max_iterations: 2, max_calls: 24, max_cost_usd: 10, max_wall_ms: 1_800_000, max_park_ms: 604_800_000 },
    });
    await gapLock(ledger, hypothesisIds[0]!);

    // Blind, not finished — and still not budget_terminated.
    await usedTurns(ledger);
    expect(ledger.projection.hunt.outcome).toBe("data_starved");
  });

  it("un-parks on an extend that grants headroom", async () => {
    const { ledger, queue, runId } = await parkedHunt();
    await steer(queue, runId, "extend", "+3 iterations");

    await boundary(ledger);

    expect(ledger.projection.hunt.budgets.max_iterations).toBe(4);
    expect(ledger.projection.hunt.status).toBe("active");
  });

  // The console sends the amount as a typed grant rather than as prose: `extend ""`
  // parsed to nothing, journaled a note saying so, and left the hunt parked at the
  // ceiling it was asking to be let past.
  it("un-parks on a typed grant, with no prose to parse", async () => {
    const { ledger, queue, runId } = await parkedHunt();
    await steer(queue, runId, "extend", "", { grant: { iterations: 3, cost_usd: 0, wall_ms: 0 } });

    await boundary(ledger);

    expect(ledger.projection.hunt.budgets.max_iterations).toBe(4);
    expect(ledger.projection.hunt.status).toBe("active");
  });

  // A grant is arithmetic on a ceiling: max_iterations + NaN is NaN, and `used >= NaN`
  // is always false, so a hunt would run on with no ceiling at all. Refused at the
  // envelope, which is where another process writes across.
  it("refuses a grant a ceiling could not survive", async () => {
    const { queue, runId } = await parkedHunt();

    await expect(
      steer(queue, runId, "extend", "", { grant: { iterations: Number.NaN, cost_usd: 0, wall_ms: 0 } }),
    ).rejects.toThrow(/finite number/);
    await expect(
      steer(queue, runId, "extend", "", { grant: { iterations: 1, cost_usd: -5, wall_ms: 0 } }),
    ).rejects.toThrow(/cost_usd/);
  });

  it("clamps an extend to the hard ceiling", async () => {
    const { ledger, queue, runId } = await parkedHunt({ termination: { hard_max_iterations: 3, hard_max_calls: 36, hard_max_cost_usd: 12 } });
    await steer(queue, runId, "extend", "+50 iterations and $500");

    await boundary(ledger);

    expect(ledger.projection.hunt.budgets.max_iterations).toBe(3);
    expect(ledger.projection.hunt.budgets.max_cost_usd).toBe(12);
  });

  // A clamped extension says it was clamped.
  it("says that it clamped", async () => {
    const { ledger, queue, runId } = await parkedHunt({ termination: { hard_max_iterations: 3, hard_max_calls: 36, hard_max_cost_usd: 12 } });
    await steer(queue, runId, "extend", "+50 iterations and $500");

    await boundary(ledger);

    expect(ledger.projection.directives.map((directive) => directive.text).join(" ")).toMatch(
      /clamped to the hard ceiling/,
    );
  });

  it("stays parked when the clamp leaves no room to run", async () => {
    const { ledger, queue, runId } = await parkedHunt({ termination: { hard_max_iterations: 1, hard_max_calls: 12, hard_max_cost_usd: 10 } });
    await steer(queue, runId, "extend", "+5 iterations");

    await boundary(ledger);

    expect(ledger.projection.hunt.status).toBe("parked");
    expect(ledger.projection.hunt.budgets.max_iterations).toBe(1);
  });

  it("keeps the hunt parked when the grant cannot be read", async () => {
    const { ledger, queue, runId } = await parkedHunt();
    await steer(queue, runId, "extend", "give it a bit more room");

    await boundary(ledger);

    expect(ledger.projection.hunt.status).toBe("parked");
    expect(ledger.projection.hunt.budgets.max_iterations).toBe(1);
    expect(ledger.projection.directives.map((directive) => directive.text).join(" ")).toMatch(/granted nothing/);
  });

  it("ends budget_terminated when the operator accepts the stop", async () => {
    const { ledger, queue, runId, hypothesisIds } = await parkedHunt();
    await steer(queue, runId, "conclude", "we are done spending on this");

    await boundary(ledger);

    // Not completed: the predicate never passed, the money ran out.
    expect(ledger.projection.hunt.outcome).toBe("budget_terminated");
    expect(ledger.projection.hypotheses.get(hypothesisIds[0]!)!.status).toBe("inconclusive");
  });

  // Two endings wore one label. Away from a ceiling nothing was exhausted, and
  // reporting an early conclude as budget_terminated told the reader a ceiling had
  // bound a hunt that stopped with turns and money to spare.
  it("ends completed, not budget_terminated, when nothing was exhausted", async () => {
    const { ledger, queue, runId, hypothesisIds } = await newLedger();
    await steer(queue, runId, "conclude", "conclude on what you have");

    await boundary(ledger);

    expect(ledger.projection.hunt.outcome).toBe("completed");
    expect(ledger.projection.hunt.termination_reason).toMatch(/an operator asked the hunt to conclude on what it had/);
    // The numbers without the claim: neither arm ran out, so neither is said to have.
    expect(ledger.projection.hunt.termination_reason).not.toMatch(/ran out|spent its allowance/);
    expect(ledger.projection.hypotheses.get(hypothesisIds[0]!)!.status).toBe("inconclusive");
  });

  it("aborts from parked, not just from active", async () => {
    const { ledger, queue, runId } = await parkedHunt();
    await steer(queue, runId, "abort", "operator halted the hunt");

    await boundary(ledger);

    expect(ledger.projection.hunt.outcome).toBe("aborted");
  });

  it("expires a hunt parked past the TTL the next time it is touched", async () => {
    const { ledger } = await parkedHunt({ termination: { park_ttl_ms: 86_400_000 } });
    vi.spyOn(Date, "now").mockReturnValue(Date.now() + 2 * 86_400_000);

    await boundary(ledger);

    // Lazy expiry: no timer ran, the hunt was simply looked at.
    expect(ledger.projection.hunt.outcome).toBe("aborted");
    expect(ledger.projection.hunt.termination_reason).toMatch(/park TTL/);
  });

  it("leaves a hunt parked inside the TTL alone", async () => {
    const { ledger } = await parkedHunt({ termination: { park_ttl_ms: 86_400_000 } });
    vi.spyOn(Date, "now").mockReturnValue(Date.now() + 3_600_000);

    await boundary(ledger);

    expect(ledger.projection.hunt.status).toBe("parked");
    expect(ledger.projection.hunt.outcome).toBeNull();
  });
});

describe("outcome precedence and coercion", () => {
  it("never downgrades an outcome already on the record", async () => {
    const { ledger } = await newLedger();
    const book = bookOf(ledger);

    book.terminate("data_starved");
    book.terminate("completed");
    expect(ledger.projection.hunt.outcome).toBe("data_starved");

    // Upward is not a downgrade: an abort discovered later is the truth.
    book.terminate("aborted");
    expect(ledger.projection.hunt.outcome).toBe("aborted");
    book.terminate("budget_terminated");
    expect(ledger.projection.hunt.outcome).toBe("aborted");
  });

  it.each(["aborted", "budget_terminated", "data_starved"] as const)(
    "coerces every still-active hypothesis to inconclusive when a hunt ends %s",
    async (outcome) => {
      const { ledger, hypothesisIds } = await newLedger({ hypotheses: ["h one", "h two"] });
      bookOf(ledger).terminate(outcome);

      const hypotheses = hypothesisIds.map((id) => ledger.projection.hypotheses.get(id)!);
      expect(hypotheses.map((hypothesis) => hypothesis.status)).toEqual(["inconclusive", "inconclusive"]);
      // "We stopped looking" is never "we cleared it".
      expect(hypotheses.some((hypothesis) => hypothesis.status === "disproven")).toBe(false);
      expect(hypotheses[0]!.resolution_reason).toMatch(new RegExp(outcome));
    },
  );

  it("leaves a verdict already on the record alone", async () => {
    const { ledger, hypothesisIds } = await newLedger({ hypotheses: ["h one", "h two"] });
    ledger.patch("hypothesis", hypothesisIds[0]!, { status: "proven", resolution_reason: "survived" });

    bookOf(ledger).terminate("aborted");

    expect(ledger.projection.hypotheses.get(hypothesisIds[0]!)!.status).toBe("proven");
    expect(ledger.projection.hypotheses.get(hypothesisIds[1]!)!.status).toBe("inconclusive");
  });
});

describe("Finalize runs on every terminal path", () => {
  const DRIVERS: [string, (ledger: Journal, ids: string[], runId: string, queue: DirectiveQueue) => Promise<void>][] = [
    [
      "completed",
      async (ledger, ids) => {
        resolve(ledger, ids[0]!);
        bookOf(ledger).conclude("nothing further to run");
      },
    ],
    [
      "data_starved",
      async (ledger, ids) => {
        await gapLock(ledger, ids[0]!);
        bookOf(ledger).conclude("nothing further to run");
      },
    ],
    [
      "budget_terminated",
      async (ledger, _ids, runId, queue) => {
        await usedTurns(ledger);
        await steer(queue, runId, "conclude", "accepted");
        await boundary(ledger);
      },
    ],
    [
      "aborted",
      async (ledger, _ids, runId, queue) => {
        await steer(queue, runId, "abort", "halted");
        await boundary(ledger);
      },
    ],
  ];

  it.each(DRIVERS)("journals exactly one report when a hunt ends %s", async (outcome, drive) => {
    const started = outcome === "budget_terminated" ? await newLedger({ budgets: CAPPED }) : await newLedger();
    await drive(started.ledger, started.hypothesisIds, started.runId, started.queue);

    expect(started.ledger.projection.hunt.outcome).toBe(outcome);

    const reports = finalized(started.ledger);
    expect(reports).toHaveLength(1);
    expect(reports[0]!.outcome).toBe(outcome);
    expect(renderReport(reports[0]!)).toMatch(new RegExp(`\\*\\*Outcome:\\*\\* ${outcome}`));
  });

  it("finalizes exactly once when a lower-precedence terminate follows", async () => {
    const { ledger } = await newLedger();
    const book = bookOf(ledger);
    book.terminate("aborted");
    book.terminate("completed");
    expect(finalized(ledger)).toHaveLength(1);
  });

  it("rebuilds the same report from the ledger alone", async () => {
    const { ledger, state, queue, runId, hypothesisIds } = await newLedger();
    await gapLock(ledger, hypothesisIds[0]!);
    bookOf(ledger).conclude("nothing further to run");
    await ledger.flush();

    // Replay-derived, so it works on any ledger rather than only on the writer's.
    const rebuilt = buildReport((await Journal.open(state, queue, runId)).projection);
    expect(rebuilt).toEqual(finalized(ledger)[0]);
  });

  it("reads as an answer when nothing was proven", async () => {
    const { ledger, hypothesisIds } = await newLedger();
    await gapLock(ledger, hypothesisIds[0]!);
    question(ledger, "was the key used elsewhere?", { entity_key: "aws_key:AKIA1", spawned_iteration: -5 });
    bookOf(ledger).conclude("nothing further to run");

    const rendered = renderReport(finalized(ledger)[0]!);
    expect(rendered).toMatch(/could not see well enough/);
    expect(rendered).toMatch(/## Visibility gaps \(3\)/);
    expect(rendered).toMatch(/## Parked backlog/);
    expect(rendered).toMatch(/was the key used elsewhere\?/);
    expect(rendered).toMatch(/Evidence strength at verdict: .* 3 open gap\(s\)/);
  });

  it("reports a parked hypothesis as backlog, not as a verdict", async () => {
    const { ledger, hypothesisIds } = await newLedger();
    resolve(ledger, hypothesisIds[0]!);
    bookOf(ledger).terminate("aborted", "operator halted the hunt");

    const report = finalized(ledger)[0]!;
    expect(report.parked_hypotheses).toHaveLength(1);
    expect(report.reason).toBe("operator halted the hunt");
  });
});

describe("termination is config", () => {
  const specWith = (thresholds: Record<string, number>, budgets?: Budgets) =>
    huntSpec({ ...huntSpecFor(), thresholds, ...(budgets ? { budgets } : {}) });

  it("ships the documented defaults and honours an override", () => {
    expect(specWith({}).termination).toEqual(DEFAULT_TERMINATION);
    expect(specWith({ priority_floor: 9 }).termination).toEqual({
      ...DEFAULT_TERMINATION,
      priority_floor: 9,
    } satisfies Termination);
  });

  it("refuses a ceiling under the budget it is meant to cap", () => {
    const over = { max_iterations: 30, max_calls: 360, max_cost_usd: 5, max_wall_ms: 1, max_park_ms: 604_800_000 };
    expect(() => specWith({ hard_max_calls: 10 }, over)).toThrow(/below budgets.max_calls/);
    expect(() => specWith({ hard_max_iterations: 10, max_iterations: 30 }, over)).toThrow(
      /below budgets.max_iterations/,
    );
    expect(() => specWith({ priority_floor: 0 })).toThrow(/must be a positive number/);
    expect(() => specWith({ floor: 5 })).toThrow(/unknown thresholds key/);
  });
});

// The regression this restores. The port aliased the hunt's budget onto the
// harness's, so one number bounded both turns and model calls; the call meter
// always won and a hunt spent 24 calls reaching iteration 2 of a "24" budget.
describe("turns and model calls are different budgets", () => {
  const specWith = (thresholds: Record<string, number>) => huntSpec({ ...huntSpecFor(), thresholds });

  it("ends the hunt on the turn count rather than the call count", async () => {
    const { ledger } = await newLedger({
      budgets: { max_iterations: 1, max_calls: 999, max_cost_usd: 999, max_wall_ms: 1_800_000, max_park_ms: 604_800_000 },
    });

    await usedTurns(ledger, 1);
    expect(boundBy(ledger.projection.hunt)).toBe("iterations");
    expect(ledger.projection.hunt.status).toBe("parked");
  });

  // Off this spec's own fan-out, not the shipped arch's: the fixture dispatches
  // one worker where threathunt.yaml dispatches four, and a ceiling read off the
  // shipped shape would be wrong for both.
  it("raises the call ceiling with the turns asked for, so calls stay a backstop", () => {
    const spec = specWith({ max_iterations: 20 });
    const perTurn = callsPerIteration(spec.dispatch.max_workers, spec.runtime.max_turns);
    expect(spec.budgets.max_calls).toBe(20 * perTurn);
    expect(perTurn).toBeGreaterThan(1);
  });

  it("leaves a call ceiling already above the derived one alone", () => {
    const spec = huntSpec({
      ...huntSpecFor({
        budgets: { max_iterations: 8, max_calls: 5_000, max_cost_usd: 3, max_wall_ms: 1, max_park_ms: 1 },
      }),
      thresholds: { max_iterations: 2 },
    });
    expect(spec.budgets.max_calls).toBe(5_000);
  });

  // The ceiling shipped equal to the default, so min(24 + 5, 24) granted nothing
  // and an operator buying headroom was told it was clamped every time.
  it("keeps the hard ceiling above the budget it caps", () => {
    expect(DEFAULT_TERMINATION.hard_max_iterations).toBeGreaterThan(DEFAULT_BUDGETS.max_iterations);
    expect(specWith({ max_iterations: 30 }).termination.hard_max_iterations).toBe(60);
  });

  // huntSpec runs again over the journaled spec on every resume, so a count read
  // only from the config would quietly restore the default mid-run.
  it("keeps the turn count a resumed run opened with", () => {
    const opened = huntSpec({
      ...huntSpecFor({
        budgets: { max_iterations: 3, max_calls: 36, max_cost_usd: 3, max_wall_ms: 1, max_park_ms: 1 },
      }),
      thresholds: {},
    });
    expect(opened.budgets.max_iterations).toBe(3);
    expect(huntSpec(opened).budgets.max_iterations).toBe(3);
  });

  it("honours a hard ceiling the config states for itself", () => {
    expect(specWith({ max_iterations: 30, hard_max_iterations: 31 }).termination.hard_max_iterations).toBe(31);
  });

  // The same fix as the turn count's, on the ceiling it was not applied to: a
  // caller raising the cost ceiling past twice the shipped default had the spec
  // refused outright, so the run never opened a ledger to be told why.
  it("rides the cost ceiling on the budget a caller asked for", () => {
    const raised = { max_iterations: 8, max_calls: 5_000, max_cost_usd: 40, max_wall_ms: 1, max_park_ms: 1 };
    const spec = huntSpec({ ...huntSpecFor({ budgets: raised }), thresholds: {} });
    expect(spec.budgets.max_cost_usd).toBe(40);
    expect(spec.termination.hard_max_cost_usd).toBe(80);
  });

  it("still honours a cost ceiling the config states for itself", () => {
    const raised = { max_iterations: 8, max_calls: 5_000, max_cost_usd: 40, max_wall_ms: 1, max_park_ms: 1 };
    const spec = huntSpec({ ...huntSpecFor({ budgets: raised }), thresholds: { hard_max_cost_usd: 41 } });
    expect(spec.termination.hard_max_cost_usd).toBe(41);
  });
});

// "budget exhausted" beside "$0.11 of $14.00" reads as a contradiction, and an
// operator seeing it reasonably concludes the ceiling is broken. What ran out was
// the turn count they set, which the sentence never named.
describe("which arm of the budget stopped the run", () => {
  const at = (iteration: number, cost_usd: number, max_iterations = 3, max_cost_usd = 14) =>
    ({ iteration, cost_usd, budgets: { max_iterations, max_cost_usd } }) as never;

  it("says turns when the turns ran out and the money did not", () => {
    expect(boundBy(at(3, 0.11))).toBe("iterations");
    expect(boundReason(at(3, 0.11))).toMatch(/ran out of turns: iteration 3 of 3/);
    // The arm with room is the answer to "then why did it stop", so it is stated too.
    expect(boundReason(at(3, 0.11))).toMatch(/having spent \$0\.1100 of \$14\.00/);
  });

  it("says spend when the money ran out first", () => {
    expect(boundBy(at(1, 14))).toBe("cost");
    expect(boundReason(at(1, 14))).toMatch(/spent its allowance: \$14\.0000 of \$14\.00/);
    expect(boundReason(at(1, 14))).toMatch(/at iteration 1 of 3/);
  });

  // Cost first when both are gone: money is the one an operator cannot get back.
  it("names spend when both arms are out", () => {
    expect(boundBy(at(3, 14))).toBe("cost");
  });

  it("names nothing while both have room", () => {
    expect(boundBy(at(1, 0.5))).toBeNull();
  });
});
