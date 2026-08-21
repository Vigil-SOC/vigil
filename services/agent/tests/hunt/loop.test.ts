import { describe, expect, it } from "vitest";
import { buildDigest } from "../../workflows/hunt/digest.js";
import { steer } from "../../workflows/hunt/inbox.js";
import { newId } from "../../workflows/hunt/ids.js";
import { Journal } from "../../workflows/hunt/journal.js";
import {
  HuntController,
  InvalidDecision,
  MAX_DECISION_ATTEMPTS,
  validateDecision,
} from "../../workflows/hunt/controller.js";
import type { DecisionProvider, WorkerDispatcher } from "../../workflows/hunt/ports.js";
import { ScriptedDecisionProvider, ScriptedWorkerDispatcher } from "../../workflows/hunt/scripted.js";
import type {
  Decision,
  DecisionAction,
  DecisionResult,
  Digest,
  DispatchRequest,
  DispatchResult,
} from "../../workflows/hunt/types.js";
import { CONCLUDE, controllerFor, INVESTIGATE, newLedger, question } from "../support/hunt.js";

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
});

describe("controller", () => {
  it("reaches a terminal state on CONCLUDE and snapshots every iteration", async () => {
    const { ledger, hypothesisIds } = await newLedger();
    // Nothing left active, so the termination predicate lets the recommendation
    // through; a CONCLUDE over an active hypothesis is refused (termination.test.ts).
    ledger.patch("hypothesis", hypothesisIds[0]!, { status: "parked" });
    const result = await controllerFor(ledger, [CONCLUDE], { costPerDecision: 0.25 }).advanceIteration();

    expect(result.hunt_status).toBe("terminal");
    expect(result.hunt_outcome).toBe("completed");
    expect(ledger.projection.decisions).toHaveLength(1);
    expect(ledger.projection.decisions[0]!.digest_presented.iteration).toBe(1);
    expect(ledger.projection.hunt.cost_usd).toBe(0.25);
  });

  it("coerces unresolved hypotheses to inconclusive, never disproven", async () => {
    const { ledger, queue, runId } = await newLedger({ hypotheses: ["h one", "h two"] });
    await steer(queue, runId, "abort", "operator halted the hunt");
    await controllerFor(ledger, [CONCLUDE]).advanceIteration();

    const statuses = [...ledger.projection.hypotheses.values()].map((h) => h.status);
    expect(statuses).toEqual(["inconclusive", "inconclusive"]);
  });

  it("never downgrades an outcome already on the record", async () => {
    const { ledger } = await newLedger();
    const controller = controllerFor(ledger, []);
    controller.terminate("aborted");
    controller.terminate("completed");
    expect(ledger.projection.hunt.outcome).toBe("aborted");
  });

  it("records a worker failure as evidence and keeps going", async () => {
    const { ledger, hypothesisIds } = await newLedger();
    const controller = controllerFor(ledger, [INVESTIGATE, CONCLUDE], {
      dispatcher: new ScriptedWorkerDispatcher([], ["threat_hunter"]),
    });
    const first = await controller.advanceIteration();

    expect(first.hunt_status).toBe("active");
    const evidence = [...ledger.projection.evidence.values()];
    expect(evidence).toHaveLength(1);
    expect(evidence[0]!.provenance).toBe("tool_failure");
    expect([...ledger.projection.dispatches.values()][0]!.status).toBe("failed");

    ledger.patch("hypothesis", hypothesisIds[0]!, { status: "parked" });
    expect((await controller.advanceIteration()).hunt_status).toBe("terminal");
  });

  it("parks at the budget checkpoint rather than ending the hunt itself", async () => {
    const { ledger } = await newLedger({ budgets: { max_iterations: 1, max_calls: 12, max_cost_usd: 10, max_wall_ms: 1_800_000, max_park_ms: 604_800_000 } });
    const result = await controllerFor(ledger, [INVESTIGATE]).advanceIteration();

    // Running out of money is a question for an operator, not a verdict.
    expect(result.hunt_status).toBe("parked");
    expect(result.hunt_outcome).toBeNull();
    // Names the arm that bound, not just "budget": a run stopped at 3 of 3 turns
    // while $0.11 of $14 was spent reads as a broken ceiling unless the sentence
    // says which of the two ran out.
    expect(result.note).toMatch(/ran out of turns: iteration 1 of 1/);
    expect(result.note).toMatch(/having spent \$/);
  });

  it("rejects an uncited ABANDON but accepts a cited one", async () => {
    const { ledger } = await newLedger();
    expect(() => validateDecision({ action: "ABANDON", rationale: "no" }, ledger.projection)).toThrow(InvalidDecision);
    expect(() =>
      validateDecision({ action: "ABANDON", rationale: "no", evidence_citations: ["ev-nope"] }, ledger.projection),
    ).toThrow(/unknown evidence/);
  });
});

describe("bounded re-prompt", () => {
  // Repeats one emission forever, so the bound is what stops the loop rather
  // than a script running out.
  class StubbornProvider implements DecisionProvider {
    readonly seenDigests: Digest[] = [];
    constructor(
      private readonly decision: Decision,
      private readonly cost = 0,
    ) {}
    async decide(digest: Digest): Promise<DecisionResult> {
      this.seenDigests.push(digest);
      return { decision: this.decision, model_id: "scripted", prompt_version: "scripted/v0", cost_usd: this.cost };
    }
  }

  const UNCITED_ABANDON: Decision = { action: "ABANDON", rationale: "dead end" };
  const OUT_OF_VOCAB = { action: "ESCALATE" as DecisionAction, rationale: "made up" };
  const DANGLING_PIVOT: Decision = {
    action: "PIVOT",
    rationale: "follow the host",
    evidence_citations: ["ev-nope"],
  };

  it.each([
    ["an uncited ABANDON", UNCITED_ABANDON, /ABANDON must cite the evidence/],
    ["an out-of-vocabulary action", OUT_OF_VOCAB, /unknown action ESCALATE/],
    ["a dangling citation", DANGLING_PIVOT, /PIVOT cites unknown evidence: ev-nope/],
  ])("re-asks after %s and accepts the correction", async (_label, bad, expected) => {
    const { ledger } = await newLedger();
    const provider = new ScriptedDecisionProvider([bad, CONCLUDE]);
    const result = await controllerFor(ledger, [], { provider }).advanceIteration();

    expect(result.action).toBe("CONCLUDE");
    expect(ledger.projection.decisions).toHaveLength(1);

    const record = ledger.projection.decisions[0]!;
    expect(record.rejected_attempts).toHaveLength(1);
    expect(record.rejected_attempts![0]).toMatch(expected);

    // The digest persisted with the accepted decision is the one that produced
    // it, so the rejection the model was shown is on the record too.
    expect(record.digest_presented.notes.join(" ")).toMatch(/previous emission was rejected/);
    expect(provider.seenDigests).toHaveLength(2);
    expect(provider.seenDigests[0]!.notes.join(" ")).not.toMatch(/previous emission was rejected/);
  });

  it("gives up after the bound and journals the stall it threw on", async () => {
    const { ledger } = await newLedger();
    const provider = new StubbornProvider(UNCITED_ABANDON, 0.02);

    await expect(new HuntController(ledger, provider).advanceIteration()).rejects.toThrow(InvalidDecision);

    expect(provider.seenDigests).toHaveLength(MAX_DECISION_ATTEMPTS);

    // The rejected emissions are on the ledger rather than only in the error,
    // and the spend that bought them is charged against the budget.
    const record = ledger.projection.decisions[0]!;
    expect(record.decision.action).toBe("STALLED");
    expect(record.rejected_attempts).toHaveLength(MAX_DECISION_ATTEMPTS);
    expect(record.cost_usd).toBeCloseTo(0.06, 10);
    expect(ledger.projection.hunt.cost_usd).toBeCloseTo(0.06, 10);

    // The iteration did not advance, so a resume retries it, and the hunt stays
    // active for the operator to do exactly that.
    expect(ledger.projection.hunt.iteration).toBe(0);
    expect(ledger.projection.hunt.status).toBe("active");
  });

  // A worker whose call dies costs one gap record and a critic whose call dies leaves
  // its hypothesis standing. The lead's used to take the whole hunt with it, so one 504
  // on one write-up threw away every iteration behind it.
  it("asks the lead again when its call dies, rather than losing the iteration", async () => {
    const { ledger } = await newLedger();
    let calls = 0;
    const provider: DecisionProvider = {
      decide: async (): Promise<DecisionResult> => {
        calls += 1;
        if (calls === 1) throw Object.assign(new Error("504 request timed out"), { cost_usd: 0.03 });
        return { decision: CONCLUDE, model_id: "m", prompt_version: "v", cost_usd: 0.01 };
      },
    };

    const iteration = await new HuntController(ledger, provider).advanceIteration();

    expect(calls).toBe(2);
    expect(iteration.action).toBe("CONCLUDE");
    const record = ledger.projection.decisions[0]!;
    expect(record.rejected_attempts).toEqual([expect.stringContaining("504")]);
    // The failed call was paid for, so the iteration is charged for both.
    expect(record.cost_usd).toBeCloseTo(0.04, 10);
  });

  it("journals a stall rather than throwing when every attempt at the lead dies", async () => {
    const { ledger } = await newLedger();
    let calls = 0;
    const provider: DecisionProvider = {
      decide: async (): Promise<DecisionResult> => {
        calls += 1;
        throw Object.assign(new Error("504 request timed out"), { cost_usd: 0.02 });
      },
    };

    await expect(new HuntController(ledger, provider).advanceIteration()).rejects.toThrow(InvalidDecision);

    expect(calls).toBe(MAX_DECISION_ATTEMPTS);
    const record = ledger.projection.decisions[0]!;
    expect(record.decision.action).toBe("STALLED");
    expect(record.cost_usd).toBeCloseTo(0.06, 10);
    // Still active: the iteration never advanced, so a resume retries it.
    expect(ledger.projection.hunt.status).toBe("active");
  });

  // A stall a lead could emit would be a way to end a hunt without a verdict.
  it("refuses a STALLED emitted by the lead", async () => {
    const { ledger } = await newLedger();
    expect(() => validateDecision({ action: "STALLED", rationale: "let me out" }, ledger.projection)).toThrow(
      /unknown action STALLED/,
    );
  });

  it("terminates a stalled hunt that has spent its budget", async () => {
    // The ceiling is stated here rather than borrowed from the shipped default:
    // the premise is "one attempt costs more than the budget", and reading the
    // default made that premise change whenever the default did.
    const { ledger } = await newLedger({
      budgets: { max_iterations: 8, max_calls: 5_000, max_cost_usd: 1, max_wall_ms: 1_800_000, max_park_ms: 604_800_000 },
    });
    const provider = new StubbornProvider(UNCITED_ABANDON, 3);

    await expect(new HuntController(ledger, provider).advanceIteration()).rejects.toThrow(InvalidDecision);

    expect(ledger.projection.hunt.status).toBe("terminal");
    expect(ledger.projection.hunt.outcome).toBe("budget_terminated");
  });

  it("charges the hunt for rejected emissions, not just the accepted one", async () => {
    const { ledger } = await newLedger();
    await controllerFor(ledger, [UNCITED_ABANDON, CONCLUDE], { costPerDecision: 0.03 }).advanceIteration();

    // Two paid calls: a rejected emission still cost money, and hunt.cost_usd
    // is the budget counter.
    expect(ledger.projection.decisions[0]!.cost_usd).toBeCloseTo(0.06, 10);
    expect(ledger.projection.hunt.cost_usd).toBeCloseTo(0.06, 10);
  });

  it("leaves rejected_attempts absent when the first emission is accepted", async () => {
    const { ledger } = await newLedger();
    await controllerFor(ledger, [CONCLUDE]).advanceIteration();
    expect(ledger.projection.decisions[0]!.rejected_attempts).toBeUndefined();
  });

  it("round-trips stated_confidence without gating on it", async () => {
    const { ledger } = await newLedger();
    await controllerFor(ledger, [{ ...CONCLUDE, stated_confidence: 0.42 }]).advanceIteration();
    expect(ledger.projection.decisions[0]!.decision.stated_confidence).toBe(0.42);
  });
});

describe("fan-out", () => {
  // Resolves in reverse order of dispatch, so completion order and request
  // order genuinely disagree.
  class OutOfOrderDispatcher implements WorkerDispatcher {
    private seen = 0;
    async dispatch(request: DispatchRequest): Promise<DispatchResult> {
      await new Promise((resolve) => setTimeout(resolve, (3 - this.seen++) * 20));
      return {
        dispatch_id: request.dispatch_id,
        evidence: [
          {
            source_system: "duckdb",
            summary: `answered: ${request.focus}`,
            payload: {},
            salience: "routine",
            why_notable: "",
            provenance: "worker",
            attacker_influenceable: false,
            instruction_like: false,
          },
        ],
        failed: false,
        failure_reason: "",
        cost_usd: 0,
      };
    }
  }

  const QUESTIONS = ["check 10.0.0.1", "check 10.0.0.2", "check 10.0.0.3"];

  // Ordered ids, because these leads are identical in every priority feature and
  // the frontier breaks that tie on the id.
  async function withQuestions(questions: string[]) {
    const started = await newLedger();
    for (const [index, text] of questions.entries()) {
      started.ledger.append({
        kind: "question",
        payload: {
          question_id: `q-${index}`,
          question: text,
          status: "open",
          entity_key: null,
          spawning_evidence_id: null,
          spawning_dispatch_id: null,
          spawned_iteration: 1,
          hypothesis_id: null,
          closed_reason: null,
        },
      });
    }
    return started.ledger;
  }

  it("dispatches one worker per open lead, capped at max_workers", async () => {
    const ledger = await withQuestions(QUESTIONS);
    const result = await controllerFor(ledger, [INVESTIGATE], {
      dispatcher: new OutOfOrderDispatcher(),
      maxWorkers: 2,
    }).advanceIteration();

    expect(result.evidence_appended).toBe(2);
    expect(ledger.projection.dispatches.size).toBe(2);
  });

  it("merges results in request order however they complete", async () => {
    const summaries = async () => {
      const ledger = await withQuestions(QUESTIONS);
      await controllerFor(ledger, [INVESTIGATE], {
        dispatcher: new OutOfOrderDispatcher(),
        maxWorkers: 3,
      }).advanceIteration();
      return [...ledger.projection.evidence.values()].map((record) => record.summary);
    };

    const expected = QUESTIONS.map((q) => `answered: ${q}`);
    expect(await summaries()).toEqual(expected);
    expect(await summaries()).toEqual(expected);
  });

  it("closes a lead once taken so it is not re-issued next iteration", async () => {
    const ledger = await withQuestions(QUESTIONS);
    const controller = controllerFor(ledger, [INVESTIGATE, INVESTIGATE], {
      dispatcher: new OutOfOrderDispatcher(),
      maxWorkers: 2,
    });

    await controller.advanceIteration();
    expect([...ledger.projection.questions.values()].filter((q) => q.status === "open")).toHaveLength(1);

    await controller.advanceIteration();
    expect([...ledger.projection.questions.values()].filter((q) => q.status === "open")).toHaveLength(0);
    // Three leads, three dispatches — none re-issued.
    expect(ledger.projection.dispatches.size).toBe(3);
  });

  it("falls back to a single worker when nothing is open to fan out over", async () => {
    const { ledger } = await newLedger();
    const result = await controllerFor(ledger, [INVESTIGATE], {
      dispatcher: new OutOfOrderDispatcher(),
      maxWorkers: 4,
    }).advanceIteration();
    expect(result.evidence_appended).toBe(1);
  });
});

describe("digest", () => {
  it("carries a weakens section and flags one-sidedness", async () => {
    const { ledger, hypothesisIds } = await newLedger();
    const hypothesisId = hypothesisIds[0]!;
    const evidenceId = newId("ev");
    ledger.append({
      kind: "evidence",
      payload: {
        evidence_id: evidenceId,
        dispatch_id: null,
        iteration: 1,
        source_system: "duckdb",
        summary: "the identity has logged in from this ASN before",
        payload: {},
        salience: "routine",
        why_notable: "",
        provenance: "worker",
        attacker_influenceable: false,
        instruction_like: false,
        entities: [],
        captured_at: new Date().toISOString(),
      },
    });

    const oneSided = buildDigest(ledger.projection, 1);
    expect(oneSided.weakens[hypothesisId]).toEqual([]);
    expect(oneSided.notes.join(" ")).toMatch(/One-sided support/);

    ledger.append({
      kind: "link",
      payload: { evidence_id: evidenceId, hypothesis_id: hypothesisId, relation: "weakens" },
    });
    const balanced = buildDigest(ledger.projection, 1);
    expect(balanced.weakens[hypothesisId]).toHaveLength(1);
    // contradicting an active hypothesis promotes salience, and code may only raise
    expect(balanced.weakens[hypothesisId]![0]!.salience).toBe("notable");
  });
});
