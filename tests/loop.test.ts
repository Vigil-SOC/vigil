import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { beforeEach, describe, expect, it } from "vitest";
import { buildDigest } from "../ai/digest.js";
import { steer } from "../ai/inbox.js";
import { Ledger, newId, snapshots } from "../ai/ledger.js";
import {
  HuntController,
  InvalidDecision,
  MAX_DECISION_ATTEMPTS,
  startHunt,
  validateDecision,
} from "../ai/loop.js";
import { ScriptedDecisionProvider, ScriptedWorkerDispatcher } from "../ai/scripted.js";
import { buildSpec, loadArch, parsePlaybook, SpecError } from "../ai/spec.js";
import type { DecisionProvider, WorkerDispatcher } from "../ai/ports.js";
import type {
  Decision,
  DecisionAction,
  DecisionResult,
  Digest,
  DispatchRequest,
  DispatchResult,
} from "../ai/types.js";

let dir: string;
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "hunt-"));
});

function ledgerFor(hypotheses = ["a credential is used from new infrastructure"]): Ledger {
  const spec = buildSpec({ prompt: hypotheses[0] });
  return startHunt({ ...spec, hypotheses }, join(dir, "run.jsonl"));
}

const INVESTIGATE: Decision = {
  action: "INVESTIGATE",
  rationale: "open a baseline query",
  query_intent: "baseline",
};
const CONCLUDE: Decision = { action: "CONCLUDE", rationale: "done" };

describe("ledger", () => {
  it("folds appends into a projection and never rewrites the file", () => {
    const ledger = ledgerFor();
    const before = readFileSync(ledger.path, "utf8");
    ledger.append({
      kind: "question",
      question: {
        question_id: newId("q", 4),
        question: "which host?",
        status: "open",
        entity_key: null,
        spawning_evidence_id: null,
        spawning_dispatch_id: null,
        spawned_iteration: 1,
        hypothesis_id: null,
      },
    });
    const after = readFileSync(ledger.path, "utf8");

    expect(after.startsWith(before)).toBe(true);
    expect(ledger.projection.questions.size).toBe(1);
    expect(Ledger.open(ledger.path).projection).toEqual(ledger.projection);
  });

  it("applies patches to the projection", () => {
    const ledger = ledgerFor();
    const id = [...ledger.projection.hypotheses.keys()][0]!;
    ledger.patch("hypothesis", id, { status: "parked" });
    expect(ledger.projection.hypotheses.get(id)!.status).toBe("parked");
  });
});

describe("controller", () => {
  it("reaches a terminal state on CONCLUDE and snapshots every iteration", async () => {
    const ledger = ledgerFor();
    // Nothing left active, so the termination predicate lets the recommendation
    // through; a CONCLUDE over an active hypothesis is refused (termination.test.ts).
    ledger.patch("hypothesis", [...ledger.projection.hypotheses.keys()][0]!, { status: "parked" });
    const controller = new HuntController(ledger, new ScriptedDecisionProvider([CONCLUDE], 0.25));
    const result = await controller.advanceIteration();

    expect(result.hunt_status).toBe("terminal");
    expect(result.hunt_outcome).toBe("completed");
    expect(ledger.projection.decisions).toHaveLength(1);
    expect(snapshots(ledger.log)[0]!.iteration).toBe(1);
    expect(ledger.projection.hunt.cost_usd).toBe(0.25);
  });

  it("coerces unresolved hypotheses to inconclusive, never disproven", async () => {
    const ledger = ledgerFor(["h one", "h two"]);
    steer(ledger.path, "abort", "operator halted the hunt");
    await new HuntController(ledger, new ScriptedDecisionProvider([CONCLUDE])).advanceIteration();
    const statuses = [...ledger.projection.hypotheses.values()].map((h) => h.status);
    expect(statuses).toEqual(["inconclusive", "inconclusive"]);
  });

  it("never downgrades an outcome already on the record", () => {
    const ledger = ledgerFor();
    const controller = new HuntController(ledger, new ScriptedDecisionProvider([]));
    controller.terminate("aborted");
    controller.terminate("completed");
    expect(ledger.projection.hunt.outcome).toBe("aborted");
  });

  it("records a worker failure as evidence and keeps going", async () => {
    const ledger = ledgerFor();
    const controller = new HuntController(
      ledger,
      new ScriptedDecisionProvider([INVESTIGATE, CONCLUDE]),
      new ScriptedWorkerDispatcher([], ["threat_hunter"]),
    );
    const first = await controller.advanceIteration();

    expect(first.hunt_status).toBe("active");
    const evidence = [...ledger.projection.evidence.values()];
    expect(evidence).toHaveLength(1);
    expect(evidence[0]!.provenance).toBe("tool_failure");
    expect([...ledger.projection.dispatches.values()][0]!.status).toBe("failed");

    ledger.patch("hypothesis", [...ledger.projection.hypotheses.keys()][0]!, { status: "parked" });
    const second = await controller.advanceIteration();
    expect(second.hunt_status).toBe("terminal");
  });

  it("parks at the budget checkpoint rather than ending the hunt itself", async () => {
    const spec = buildSpec({ prompt: "h" });
    const ledger = startHunt(
      { ...spec, budgets: { max_iterations: 1, max_cost_usd: 10 } },
      join(dir, "run.jsonl"),
    );
    const result = await new HuntController(ledger, new ScriptedDecisionProvider([INVESTIGATE])).advanceIteration();

    // Running out of money is a question for an operator, not a verdict.
    expect(result.hunt_status).toBe("parked");
    expect(result.hunt_outcome).toBeNull();
    expect(result.note).toMatch(/budget exhausted/);
  });

  it("rejects an uncited ABANDON but accepts a cited one", () => {
    const ledger = ledgerFor();
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
    const ledger = ledgerFor();
    const provider = new ScriptedDecisionProvider([bad, CONCLUDE]);
    const result = await new HuntController(ledger, provider).advanceIteration();

    expect(result.action).toBe("CONCLUDE");
    expect(ledger.projection.decisions).toHaveLength(1);

    const record = ledger.projection.decisions[0]!;
    expect(record.rejected_attempts).toHaveLength(1);
    expect(record.rejected_attempts![0]).toMatch(expected);

    // The digest persisted with the accepted decision is the one that produced
    // it, so the rejection the model was shown is on the record too.
    expect(snapshots(ledger.log)[0]!.notes.join(" ")).toMatch(/previous emission was rejected/);
    expect(provider.seenDigests).toHaveLength(2);
    expect(provider.seenDigests[0]!.notes.join(" ")).not.toMatch(/previous emission was rejected/);
  });

  it("gives up after the bound and journals the stall it threw on", async () => {
    const ledger = ledgerFor();
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

  // A stall a lead could emit would be a way to end a hunt without a verdict.
  it("refuses a STALLED emitted by the lead", () => {
    const ledger = ledgerFor();
    expect(() =>
      validateDecision({ action: "STALLED", rationale: "let me out" }, ledger.projection),
    ).toThrow(/unknown action STALLED/);
  });

  it("terminates a stalled hunt that has spent its budget", async () => {
    const ledger = ledgerFor();
    // Each attempt costs more than the whole budget allows.
    const provider = new StubbornProvider(UNCITED_ABANDON, 3);

    await expect(new HuntController(ledger, provider).advanceIteration()).rejects.toThrow(InvalidDecision);

    expect(ledger.projection.hunt.status).toBe("terminal");
    expect(ledger.projection.hunt.outcome).toBe("budget_terminated");
  });

  it("charges the hunt for rejected emissions, not just the accepted one", async () => {
    const ledger = ledgerFor();
    const provider = new ScriptedDecisionProvider([UNCITED_ABANDON, CONCLUDE], 0.03);
    await new HuntController(ledger, provider).advanceIteration();

    // Two paid calls: a rejected emission still cost money, and hunt.cost_usd
    // is the budget counter.
    expect(ledger.projection.decisions[0]!.cost_usd).toBeCloseTo(0.06, 10);
    expect(ledger.projection.hunt.cost_usd).toBeCloseTo(0.06, 10);
  });

  it("leaves rejected_attempts absent when the first emission is accepted", async () => {
    const ledger = ledgerFor();
    await new HuntController(ledger, new ScriptedDecisionProvider([CONCLUDE])).advanceIteration();
    expect(ledger.projection.decisions[0]!.rejected_attempts).toBeUndefined();
  });

  it("round-trips stated_confidence without gating on it", async () => {
    const ledger = ledgerFor();
    const confident: Decision = { ...CONCLUDE, stated_confidence: 0.42 };
    await new HuntController(ledger, new ScriptedDecisionProvider([confident])).advanceIteration();
    expect(ledger.projection.decisions[0]!.decision.stated_confidence).toBe(0.42);
  });
});

describe("fan-out", () => {
  // Resolves in reverse order of dispatch, so completion order and request
  // order genuinely disagree.
  class OutOfOrderDispatcher implements WorkerDispatcher {
    private seen = 0;
    async dispatch(request: DispatchRequest): Promise<DispatchResult> {
      const delay = (3 - this.seen++) * 20;
      await new Promise((resolve) => setTimeout(resolve, delay));
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

  // Ordered ids, because these leads are identical in every priority feature and
  // the frontier breaks that tie on the id.
  function withQuestions(questions: string[]): Ledger {
    const ledger = ledgerFor();
    for (const [index, question] of questions.entries()) {
      ledger.append({
        kind: "question",
        question: {
          question_id: `q-${index}`,
          question,
          status: "open",
          entity_key: null,
          spawning_evidence_id: null,
          spawning_dispatch_id: null,
          spawned_iteration: 1,
          hypothesis_id: null,
        },
      });
    }
    return ledger;
  }

  const QUESTIONS = ["check 10.0.0.1", "check 10.0.0.2", "check 10.0.0.3"];

  it("dispatches one worker per open lead, capped at max_workers", async () => {
    const ledger = withQuestions(QUESTIONS);
    const result = await new HuntController(
      ledger,
      new ScriptedDecisionProvider([INVESTIGATE]),
      new OutOfOrderDispatcher(),
      { mode: "parallel", fan_out_over: "questions", max_workers: 2 },
    ).advanceIteration();

    expect(result.evidence_appended).toBe(2);
    expect(ledger.projection.dispatches.size).toBe(2);
  });

  it("merges results in request order however they complete", async () => {
    const summaries = async () => {
      const ledger = withQuestions(QUESTIONS);
      await new HuntController(ledger, new ScriptedDecisionProvider([INVESTIGATE]), new OutOfOrderDispatcher(), {
        mode: "parallel",
        fan_out_over: "questions",
        max_workers: 3,
      }).advanceIteration();
      return [...ledger.projection.evidence.values()].map((record) => record.summary);
    };

    const expected = QUESTIONS.map((question) => `answered: ${question}`);
    expect(await summaries()).toEqual(expected);
    expect(await summaries()).toEqual(expected);
  });

  it("closes a lead once taken so it is not re-issued next iteration", async () => {
    const ledger = withQuestions(QUESTIONS);
    const controller = new HuntController(
      ledger,
      new ScriptedDecisionProvider([INVESTIGATE, INVESTIGATE]),
      new OutOfOrderDispatcher(),
      { mode: "parallel", fan_out_over: "questions", max_workers: 2 },
    );

    await controller.advanceIteration();
    expect([...ledger.projection.questions.values()].filter((q) => q.status === "open")).toHaveLength(1);

    await controller.advanceIteration();
    expect([...ledger.projection.questions.values()].filter((q) => q.status === "open")).toHaveLength(0);
    // Three leads, three dispatches — none re-issued.
    expect(ledger.projection.dispatches.size).toBe(3);
  });

  it("falls back to a single worker when nothing is open to fan out over", async () => {
    const ledger = ledgerFor();
    const result = await new HuntController(
      ledger,
      new ScriptedDecisionProvider([INVESTIGATE]),
      new OutOfOrderDispatcher(),
      { mode: "parallel", fan_out_over: "questions", max_workers: 4 },
    ).advanceIteration();
    expect(result.evidence_appended).toBe(1);
  });

  it("rejects a nonsense dispatch policy at arch load", () => {
    expect(() => loadArch("tests/fixtures/bad-dispatch.yaml")).toThrow(/serial or parallel/);
  });
});

describe("digest", () => {
  it("carries a weakens section and flags one-sidedness", () => {
    const ledger = ledgerFor();
    const hypothesisId = [...ledger.projection.hypotheses.keys()][0]!;
    const evidenceId = newId("ev");
    ledger.append({
      kind: "evidence",
      evidence: {
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

    ledger.append({ kind: "link", link: { evidence_id: evidenceId, hypothesis_id: hypothesisId, relation: "weakens" } });
    const balanced = buildDigest(ledger.projection, 1);
    expect(balanced.weakens[hypothesisId]).toHaveLength(1);
    // contradicting an active hypothesis promotes salience, and code may only raise
    expect(balanced.weakens[hypothesisId]![0]!.salience).toBe("notable");
  });
});

describe("spec", () => {
  it("reads a playbook from front matter, keeping the body as narrative", () => {
    const playbook = parsePlaybook("---\nname: base\nhypotheses:\n  - one\n---\nnarrative here");
    expect(playbook.hypotheses).toEqual(["one"]);
    expect(playbook.narrative).toBe("narrative here");
  });

  it("refuses a hunt with nothing to test", () => {
    expect(() => buildSpec({ entity: "10.0.0.1" })).toThrow(SpecError);
  });

  it("types a seed entity", () => {
    expect(buildSpec({ prompt: "q", entity: "10.0.0.1" }).scope["entity"]).toEqual({ type: "ip", value: "10.0.0.1" });
    expect(buildSpec({ prompt: "q", entity: "host:web-01" }).scope["entity"]).toEqual({ type: "host", value: "web-01" });
  });
});
