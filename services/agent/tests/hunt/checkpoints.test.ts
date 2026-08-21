import { describe, expect, it } from "vitest";
import {
  AUTO_ACTOR,
  DEFAULT_CHECKPOINTS,
  pendingCheckpoints,
  resolutionOf,
} from "../../workflows/hunt/checkpoints.js";
import { DEFAULT_VERDICTS } from "../../workflows/hunt/config.js";
import { HuntParked, validateDecision } from "../../workflows/hunt/controller.js";
import { buildDigest, scoredFrontier, suppressedEntities } from "../../workflows/hunt/digest.js";
import { steer } from "../../workflows/hunt/inbox.js";
import type { Journal } from "../../workflows/hunt/journal.js";
import type { DirectiveQueue, Enricher, WorkerDispatcher } from "../../workflows/hunt/ports.js";
import { renderReport } from "../../workflows/hunt/report.js";
import {
  ScriptedDecisionProvider,
  ScriptedDisconfirmationCritic,
  ScriptedWorkerDispatcher,
  type ScriptedDecision,
} from "../../workflows/hunt/scripted.js";
import { evidenceStrength, openGaps } from "../../workflows/hunt/strength.js";
import type { Decision, DispatchRequest, DispatchResult, Entity } from "../../workflows/hunt/types.js";
import {
  CONCLUDE,
  controllerFor,
  evidenceOn,
  finalized,
  INVESTIGATE,
  newLedger,
  provable,
  question,
  reopen,
  SEED_IP,
  validateOn as validate,
  type Started,
} from "../support/hunt.js";

const SEED_KEY = `${SEED_IP.type}:${SEED_IP.value}`;
const seen = (ledger: Journal) => ledger.projection.directives.map((directive) => directive.text).join(" ");

describe("the ledger is the authority, the console is delivery", () => {
  it("journals an auto resolution rather than silently skipping the checkpoint", async () => {
    const { ledger, hypothesisIds } = await newLedger({ checkpoints: { verdict_review: "auto" } });
    const citations = provable(ledger, hypothesisIds[0]!);

    await controllerFor(ledger, [validate(hypothesisIds[0]!, citations)], {
      critic: new ScriptedDisconfirmationCritic(true),
    }).advanceIteration();

    // The verdict landed exactly as it does with no checkpoint machinery at all…
    expect(ledger.projection.hypotheses.get(hypothesisIds[0]!)!.status).toBe("proven");

    // …and the ledger says who approved it, which happens to be nobody.
    const [checkpoint] = [...ledger.projection.checkpoints.values()].filter(
      (entry) => entry.checkpoint_class === "verdict_review",
    );
    const resolution = resolutionOf(ledger.projection, checkpoint!.checkpoint_id)!;
    expect(resolution.answer).toBe("approve");
    expect(resolution.actor).toBe(AUTO_ACTOR);
    expect(resolution.directive_id).toBeNull();
    expect(pendingCheckpoints(ledger.projection)).toHaveLength(0);
  });

  it("keeps a pending checkpoint out of the operator directive stream", async () => {
    // Nothing the controller journals for itself may look like operator input:
    // the drain counts operator directives to know what it has already taken.
    const { ledger, hypothesisIds } = await newLedger({ checkpoints: { verdict_review: "ask" } });
    const citations = provable(ledger, hypothesisIds[0]!);

    await controllerFor(ledger, [validate(hypothesisIds[0]!, citations)], {
      critic: new ScriptedDisconfirmationCritic(true),
    }).advanceIteration();

    expect(ledger.projection.directives.filter((directive) => directive.origin !== "controller")).toHaveLength(0);
    expect(pendingCheckpoints(ledger.projection)).toHaveLength(1);
  });
});

describe("verdict review", () => {
  async function parkedOnVerdict() {
    const started = await newLedger({ checkpoints: { verdict_review: "ask" } });
    const hypothesisId = started.hypothesisIds[0]!;
    const citations = provable(started.ledger, hypothesisId);
    const result = await controllerFor(started.ledger, [validate(hypothesisId, citations)], {
      critic: new ScriptedDisconfirmationCritic(true),
    }).advanceIteration();

    expect(result.hunt_status).toBe("parked");
    return {
      started,
      hypothesisId,
      checkpointId: pendingCheckpoints(started.ledger.projection)[0]!.checkpoint_id,
    };
  }

  it("parks instead of proving, and refuses to step until it is answered", async () => {
    const { started, hypothesisId } = await parkedOnVerdict();

    expect(started.ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
    expect(started.ledger.projection.hunt.outcome).toBeNull();

    await expect(controllerFor(started.ledger, [CONCLUDE]).advanceIteration()).rejects.toThrow(HuntParked);
    await expect(controllerFor(started.ledger, [CONCLUDE]).advanceIteration()).rejects.toThrow(/approve .*reject/s);
  });

  it("survives process death: a controller from the ledger alone still shows it pending", async () => {
    const { started, checkpointId } = await parkedOnVerdict();

    // Nothing of this process carries over — the ledger is the whole state.
    const reopened = await reopen(started);
    expect(pendingCheckpoints(reopened.projection).map((c) => c.checkpoint_id)).toEqual([checkpointId]);
    expect(reopened.projection.hunt.status).toBe("parked");
    expect(reopened.projection.hunt.parked_reason).toContain(checkpointId);
  });

  it("applies the patch VALIDATE computed, with the strength snapshot from then", async () => {
    const { started, hypothesisId, checkpointId } = await parkedOnVerdict();
    const atValidateTime = [...started.ledger.projection.checkpoints.values()][1]!.context![
      "evidence_strength"
    ] as Record<string, unknown>;

    await steer(started.queue, started.runId, "approve", "reviewed the payloads, this holds", { checkpoint_id: checkpointId });
    const resumed = await reopen(started);
    await controllerFor(resumed, [INVESTIGATE]).advanceIteration();

    // The snapshot is the one the reviewer was shown, never one recomputed on
    // approval: the same verdict delivered late, not a better-informed one.
    const hypothesis = resumed.projection.hypotheses.get(hypothesisId)!;
    expect(hypothesis.status).toBe("proven");
    expect(hypothesis.evidence_strength).toEqual(atValidateTime);
    expect(hypothesis.evidence_strength!.corroborating_sources).toBe(2);
  });

  it("refuses to land a verdict the argue-the-null pass no longer covers", async () => {
    const { started, hypothesisId, checkpointId } = await parkedOnVerdict();

    // Support the critic never argued against arrives while the review waits, so
    // the stored patch's "survived disconfirmation" is no longer true by landing.
    evidenceOn(started.ledger, hypothesisId, { source: "okta" });
    expect(evidenceStrength(started.ledger.projection, hypothesisId).survived_disconfirmation).toBe(false);

    await steer(started.queue, started.runId, "approve", "reviewed the payloads, this holds", { checkpoint_id: checkpointId });
    const resumed = await reopen(started);
    await controllerFor(resumed, [INVESTIGATE]).advanceIteration();

    expect(resumed.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
    expect(seen(resumed)).toMatch(/no longer carries it.*VALIDATE it again/s);
  });

  it("closes inconclusive when the operator declares a gap and then approves", async () => {
    const { started, hypothesisId, checkpointId } = await parkedOnVerdict();

    // A reviewer says the hunt is blind somewhere and approves in the same breath.
    // The gap they just declared must not be the one thing the approval ignores.
    for (const blind of ["no EDR on that subnet", "no CloudTrail before August", "netflow sampled at 1:100"]) {
      await steer(started.queue, started.runId, "gap", blind, { hypothesis_id: hypothesisId });
    }
    await steer(started.queue, started.runId, "approve", "looks right to me", { checkpoint_id: checkpointId });

    const resumed = await reopen(started);
    await controllerFor(resumed, [INVESTIGATE]).advanceIteration();

    const hypothesis = resumed.projection.hypotheses.get(hypothesisId)!;
    expect(hypothesis.status).toBe("inconclusive");
    expect(hypothesis.resolution_reason).toMatch(/gap-locked before the approved verdict landed/);
    // The numbers on the record are the ones that closed it, not the ones the
    // reviewer was shown — a verdict nobody can re-read is not auditable.
    expect(hypothesis.evidence_strength!.open_gaps).toBe(3);
  });

  it("leaves the hypothesis active on a rejection, with the reason in the next digest", async () => {
    const { started, hypothesisId, checkpointId } = await parkedOnVerdict();
    await steer(started.queue, started.runId, "reject", "the second source is the same collector under another name", {
      checkpoint_id: checkpointId,
    });

    const provider = new ScriptedDecisionProvider([INVESTIGATE]);
    await controllerFor(started.ledger, [], { provider }).advanceIteration();

    expect(started.ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
    expect(started.ledger.projection.hunt.status).toBe("active");
    expect(provider.seenDigests[0]!.directives.join(" ")).toMatch(/same collector under another name/);
  });

  it("answers a checkpoint by id, so a stale or duplicate answer changes nothing", async () => {
    const { started, hypothesisId, checkpointId } = await parkedOnVerdict();
    await steer(started.queue, started.runId, "reject", "not yet", { checkpoint_id: checkpointId });
    await steer(started.queue, started.runId, "approve", "changed my mind", { checkpoint_id: checkpointId });

    await controllerFor(started.ledger, [INVESTIGATE]).advanceIteration();

    // The first answer stands, and the second is on the record as ignored.
    expect(started.ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
    expect(resolutionOf(started.ledger.projection, checkpointId)!.answer).toBe("reject");
    expect(seen(started.ledger)).toMatch(/first answer stands/);
  });

  it("reviews the conclusion too, and terminates through the one funnel on approval", async () => {
    const started = await newLedger({ checkpoints: { verdict_review: "ask" } });
    started.ledger.patch("hypothesis", started.hypothesisIds[0]!, {
      status: "parked",
      resolution_reason: "dropped",
    });

    const parked = await controllerFor(started.ledger, [CONCLUDE]).advanceIteration();
    expect(parked.hunt_status).toBe("parked");
    expect(started.ledger.projection.hunt.outcome).toBeNull();

    const checkpointId = pendingCheckpoints(started.ledger.projection)[0]!.checkpoint_id;
    await steer(started.queue, started.runId, "approve", "agreed, we are done", { checkpoint_id: checkpointId });
    const result = await controllerFor(started.ledger, []).advanceIteration();

    expect(result.hunt_outcome).toBe("completed");
    // Finalize sits on terminate(), so an approval that ends a hunt still
    // produces the deliverable.
    expect(finalized(started.ledger)).toHaveLength(1);
  });

  it("keeps the hunt running when the conclusion is refused", async () => {
    const started = await newLedger({ checkpoints: { verdict_review: "ask" } });
    started.ledger.patch("hypothesis", started.hypothesisIds[0]!, {
      status: "parked",
      resolution_reason: "dropped",
    });
    await controllerFor(started.ledger, [CONCLUDE]).advanceIteration();

    const checkpointId = pendingCheckpoints(started.ledger.projection)[0]!.checkpoint_id;
    await steer(started.queue, started.runId, "reject", "check the second host first", { checkpoint_id: checkpointId });
    const result = await controllerFor(started.ledger, [INVESTIGATE]).advanceIteration();

    expect(result.hunt_status).toBe("active");
    expect(started.ledger.projection.hunt.outcome).toBeNull();
    expect(finalized(started.ledger)).toHaveLength(0);
  });
});

describe("the start approval", () => {
  it("holds the hunt at pending_approval until it is journaled", async () => {
    const started = await newLedger({ checkpoints: { hypothesis_approval: "ask" } });

    expect(started.ledger.projection.hunt.status).toBe("pending_approval");
    expect(pendingCheckpoints(started.ledger.projection)[0]!.checkpoint_class).toBe("hypothesis_approval");
    await expect(controllerFor(started.ledger, [INVESTIGATE]).advanceIteration()).rejects.toThrow(HuntParked);

    const checkpointId = pendingCheckpoints(started.ledger.projection)[0]!.checkpoint_id;
    await steer(started.queue, started.runId, "approve", "reviewed the hypotheses", { checkpoint_id: checkpointId });
    const result = await controllerFor(started.ledger, [INVESTIGATE]).advanceIteration();

    expect(result.hunt_status).toBe("active");
    expect(resolutionOf(started.ledger.projection, checkpointId)!.actor).not.toBe(AUTO_ACTOR);
  });

  it("aborts a rejected start through terminate(), so it still finalizes", async () => {
    const started = await newLedger({ checkpoints: { hypothesis_approval: "ask" } });
    const checkpointId = pendingCheckpoints(started.ledger.projection)[0]!.checkpoint_id;
    await steer(started.queue, started.runId, "reject", "wrong scope, start again", { checkpoint_id: checkpointId });

    const result = await controllerFor(started.ledger, [INVESTIGATE]).advanceIteration();

    expect(result.hunt_outcome).toBe("aborted");
    expect(started.ledger.projection.hunt.termination_reason).toMatch(/rejected the hypotheses/);
    // Never disproven: nobody looked.
    expect(started.ledger.projection.hypotheses.get(started.hypothesisIds[0]!)!.status).toBe("inconclusive");
    expect(finalized(started.ledger)).toHaveLength(1);
    expect(renderReport(finalized(started.ledger)[0]!)).toMatch(/\*\*Outcome:\*\* aborted/);
  });

  it("starts active under the auto policy, with the approval on the record", async () => {
    const { ledger } = await newLedger({ checkpoints: { hypothesis_approval: "auto" } });

    expect(ledger.projection.hunt.status).toBe("active");
    expect(pendingCheckpoints(ledger.projection)).toHaveLength(0);
    expect(ledger.projection.resolutions[0]!.actor).toBe(AUTO_ACTOR);
    // The default, so a headless run has nothing pending and no TTY to prompt on.
    expect(DEFAULT_CHECKPOINTS.hypothesis_approval).toBe("auto");
  });
});

describe("the soft directive set", () => {
  it("binds the Hunt Lead rather than only the digest", async () => {
    const started = await newLedger();
    evidenceOn(started.ledger, started.hypothesisIds[0]!, { source: "duckdb", entities: [SEED_IP] });
    await steer(started.queue, started.runId, "benign", "our own scanner", { entity_key: SEED_KEY });
    await controllerFor(started.ledger, [INVESTIGATE]).advanceIteration();

    // Dropping it from pivot candidates only makes the lead less likely to name
    // it. An authorization the lead can decline to notice is a suggestion.
    const citations = [...started.ledger.projection.evidence.keys()];
    for (const action of ["INVESTIGATE", "DEEPEN", "PIVOT"] as const) {
      expect(() =>
        validateDecision(
          { action, rationale: "chase it anyway", target_entity: SEED_KEY, evidence_citations: citations },
          started.ledger.projection,
        ),
      ).toThrow(/known-benign/);
    }

    // ABANDON is the exception: closing work on a suppressed entity is the
    // point of suppressing it.
    expect(() =>
      validateDecision(
        {
          action: "ABANDON",
          rationale: "the operator cleared it",
          target_entity: SEED_KEY,
          evidence_citations: citations,
        },
        started.ledger.projection,
      ),
    ).not.toThrow();
  });

  it("suppresses an entity without touching a single record, and lets a revoke lift it", async () => {
    const started = await newLedger();
    const evidenceId = evidenceOn(started.ledger, started.hypothesisIds[0]!, {
      source: "duckdb",
      entities: [SEED_IP],
    });
    const before = started.ledger.projection.evidence.get(evidenceId)!;

    await steer(started.queue, started.runId, "benign", "our own scanner", { entity_key: SEED_KEY });
    await controllerFor(started.ledger, [INVESTIGATE]).advanceIteration();

    expect([...suppressedEntities(started.ledger.projection).keys()]).toEqual([SEED_KEY]);
    // The evidence is exactly what it was: a suppression is an authorization,
    // not a deletion.
    expect(started.ledger.projection.evidence.get(evidenceId)).toEqual(before);

    const digest = buildDigest(started.ledger.projection, 2);
    expect(digest.entities.find((entity) => entity.value === SEED_IP.value)!.suppressed).toBe(true);
    expect(digest.notes.join(" ")).toMatch(/known-benign/);

    await steer(started.queue, started.runId, "benign", "put it back in play", { entity_key: SEED_KEY, revoke: true });
    await controllerFor(started.ledger, [INVESTIGATE]).advanceIteration();

    expect(suppressedEntities(started.ledger.projection).size).toBe(0);
    expect(
      buildDigest(started.ledger.projection, 3).entities.find((entity) => entity.value === SEED_IP.value)!.suppressed,
    ).toBeUndefined();
  });

  it("keeps a suppressed entity out of enrichment and out of the pivot candidates", async () => {
    const started = await newLedger();
    const enriched: string[] = [];
    const enricher: Enricher = async (entity: Entity) => {
      enriched.push(`${entity.type}:${entity.value}`);
      return [];
    };

    await steer(started.queue, started.runId, "benign", "our own scanner", { entity_key: SEED_KEY });
    await controllerFor(started.ledger, [INVESTIGATE], {
      enricher,
      dispatcher: new ScriptedWorkerDispatcher([
        {
          source_system: "duckdb",
          summary: `10.0.0.5 talked to ${SEED_IP.value}`,
          payload: { src_ip: "10.0.0.5", dest_ip: SEED_IP.value },
          salience: "routine",
          why_notable: "",
          provenance: "worker",
          attacker_influenceable: false,
          instruction_like: false,
        },
      ]),
    }).advanceIteration();

    expect(enriched).toContain("ip:10.0.0.5");
    expect(enriched).not.toContain(SEED_KEY);

    // And it is not offered as somewhere to pivot to, having been cleared once.
    const digest = buildDigest(started.ledger.projection, 2);
    expect(digest.pivot_candidates.map((entity) => entity.value)).not.toContain(SEED_IP.value);
  });

  it("counts an operator-declared gap like a tool failure, up to gap-lock", async () => {
    const started = await newLedger();
    const hypothesisId = started.hypothesisIds[0]!;
    const citations = provable(started.ledger, hypothesisId);

    for (const text of ["no EDR on the 10.30.0.0/16 subnet", "no DNS logging before 03:00", "no proxy logs at all"]) {
      await steer(started.queue, started.runId, "gap", text, { hypothesis_id: hypothesisId });
    }
    await controllerFor(started.ledger, [INVESTIGATE]).advanceIteration();

    expect(openGaps(started.ledger.projection, hypothesisId)).toBe(DEFAULT_VERDICTS.gap_lock_threshold);

    // Enough blindness that the verdict closes inconclusive: the hunt could not
    // look, which is never the same as having cleared it.
    const result = await controllerFor(started.ledger, [validate(hypothesisId, citations)], {
      critic: new ScriptedDisconfirmationCritic(true),
    }).advanceIteration();

    expect(started.ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("inconclusive");
    expect(result.note).toMatch(/gap-locked/);
  });

  it("pins a boosted question to the top of the frontier", async () => {
    const started = await newLedger();
    question(started.ledger, "the obvious next thread", { entity_key: "ip:10.0.0.1" });
    // Stale enough to rank below it on recency, so the pin is doing the work
    // rather than a tie-break.
    const buried = question(started.ledger, "a thread nobody ranked", { spawned_iteration: -5 });

    expect(scoredFrontier(started.ledger.projection, 1)[0]!.question.question_id).not.toBe(buried);

    await steer(started.queue, started.runId, "boost", "look at this one next", { question_id: buried });
    await controllerFor(started.ledger, [INVESTIGATE]).advanceIteration();

    const [top] = scoredFrontier(started.ledger.projection, 2);
    expect(top!.question.question_id).toBe(buried);
    expect(top!.boosted).toBe(true);
    // Pinned, not rescored: the floor termination measures against still means
    // what it meant.
    expect(top!.score).toBeLessThan(scoredFrontier(started.ledger.projection, 2)[1]!.score);
  });

  it("records a premise correction as the note it already is", async () => {
    const started = await newLedger();
    await steer(started.queue, started.runId, "note", "the 03:00 spike is our backup window, not exfil");

    const provider = new ScriptedDecisionProvider([INVESTIGATE]);
    await controllerFor(started.ledger, [], { provider }).advanceIteration();

    expect(provider.seenDigests[0]!.directives.join(" ")).toMatch(/backup window/);
  });
});

describe("a hard abort preempts the work in flight", () => {
  // Queues the halt while the first worker is running, which is what an
  // operator hitting abort mid-iteration actually looks like.
  class AbortingDispatcher implements WorkerDispatcher {
    readonly seen: string[] = [];
    constructor(
      private readonly queue: DirectiveQueue,
      private readonly runId: string,
    ) {}

    async dispatch(request: DispatchRequest): Promise<DispatchResult> {
      this.seen.push(request.focus);
      if (this.seen.length === 1) await steer(this.queue, this.runId, "abort", "operator halted the hunt");
      return { dispatch_id: request.dispatch_id, evidence: [], failed: false, failure_reason: "", cost_usd: 0 };
    }
  }

  it("skips the workers that had not started, journals why, and aborts through terminate()", async () => {
    const started = await newLedger();
    for (const text of ["check 10.0.0.1", "check 10.0.0.2", "check 10.0.0.3"]) question(started.ledger, text);

    const dispatcher = new AbortingDispatcher(started.queue, started.runId);
    await controllerFor(started.ledger, [INVESTIGATE, INVESTIGATE], { dispatcher }).advanceIteration();

    // One worker ran; the other two never did, and the ledger says so rather
    // than leaving two dispatches that look like they chose not to look.
    expect(dispatcher.seen).toHaveLength(1);
    const skipped = [...started.ledger.projection.dispatches.values()].filter(
      (dispatch) => dispatch.failure_reason?.includes("abort") === true,
    );
    expect(skipped).toHaveLength(2);
    expect(skipped.every((dispatch) => dispatch.status === "failed")).toBe(true);

    // The boundary still owns the ending: aborted, coerced, finalized.
    const result = await controllerFor(started.ledger, []).advanceIteration();
    expect(result.hunt_outcome).toBe("aborted");
    expect([...started.ledger.projection.hypotheses.values()].every((h) => h.status === "inconclusive")).toBe(true);
    expect(finalized(started.ledger)).toHaveLength(1);
  });
});

describe("scope", () => {
  it("refuses a cross-tenant lead outright rather than raising a checkpoint", async () => {
    const started = await newLedger({ checkpoints: { scope_extension: "ask" }, scope: { tenant: "frothly" } });
    await steer(started.queue, started.runId, "lead", "check tenant:acme for the same key");

    const result = await controllerFor(started.ledger, [INVESTIGATE]).advanceIteration();

    // No checkpoint: a tenant boundary is not one an operator may waive from
    // inside the hunt, so there is nothing to approve.
    expect(pendingCheckpoints(started.ledger.projection)).toHaveLength(0);
    expect(result.hunt_status).toBe("active");
    expect([...started.ledger.projection.questions.values()]).toHaveLength(0);
    expect(seen(started.ledger)).toMatch(/names tenant acme.*Refused outright/s);
  });

  it("asks before growing past the declared scope, and grows on approval", async () => {
    const started = await newLedger({
      checkpoints: { scope_extension: "ask" },
      scope: { tenant: "frothly", entities: ["ip:10.0.0.5"] },
    });
    await steer(started.queue, started.runId, "lead", `pull on ${SEED_IP.value} as well`);

    await expect(controllerFor(started.ledger, [INVESTIGATE]).advanceIteration()).rejects.toThrow(HuntParked);
    const checkpoint = pendingCheckpoints(started.ledger.projection)[0]!;
    expect(checkpoint.checkpoint_class).toBe("scope_extension");
    expect(checkpoint.question).toMatch(new RegExp(SEED_KEY.replace(/\./g, "\\.")));
    expect([...started.ledger.projection.questions.values()]).toHaveLength(0);

    await steer(started.queue, started.runId, "approve", "yes, it is ours", { checkpoint_id: checkpoint.checkpoint_id });
    const result = await controllerFor(started.ledger, [INVESTIGATE]).advanceIteration();

    expect(result.hunt_status).toBe("active");
    expect([...started.ledger.projection.questions.values()].map((entry) => entry.question)).toEqual([
      `pull on ${SEED_IP.value} as well`,
    ]);
    expect(started.ledger.projection.hunt.scope["entities"]).toEqual(["ip:10.0.0.5", SEED_KEY]);
  });

  it("leaves a hunt that declared no scope free of scope checkpoints", async () => {
    const started = await newLedger({ checkpoints: { scope_extension: "ask" } });
    await steer(started.queue, started.runId, "lead", `check ${SEED_IP.value}`);

    const result = await controllerFor(started.ledger, [INVESTIGATE]).advanceIteration();

    expect(result.hunt_status).toBe("active");
    expect(pendingCheckpoints(started.ledger.projection)).toHaveLength(0);
    expect([...started.ledger.projection.questions.values()].map((entry) => entry.question)).toEqual([
      `check ${SEED_IP.value}`,
    ]);
  });

  it("does not treat the seed entity as a boundary", async () => {
    // A hunt seeded with one entity is *about* it, not fenced to it: following
    // the trail somewhere new is the job, not a scope extension.
    const started = await newLedger({
      checkpoints: { scope_extension: "ask" },
      scope: { entity: { type: "ip", value: "10.0.0.5" } },
    });
    await steer(started.queue, started.runId, "lead", `check what else ${SEED_IP.value} talked to`);

    const result = await controllerFor(started.ledger, [INVESTIGATE]).advanceIteration();

    expect(result.hunt_status).toBe("active");
    expect(pendingCheckpoints(started.ledger.projection)).toHaveLength(0);
  });
});

describe("HANDOFF_IR", () => {
  const handoff = (hypothesisId: string | null): Decision => ({
    action: "HANDOFF_IR",
    rationale: "the key is being used right now; IR should rotate it",
    target_hypothesis_id: hypothesisId,
  });

  it("escalates a proven hypothesis, journals the case file, and keeps hunting", async () => {
    const started = await newLedger({
      checkpoints: { verdict_review: "auto" },
      hypotheses: ["h one", "h two"],
    });
    const citations = provable(started.ledger, started.hypothesisIds[0]!);

    const controller = controllerFor(
      started.ledger,
      [validate(started.hypothesisIds[0]!, citations), handoff(started.hypothesisIds[0]!), CONCLUDE],
      { critic: new ScriptedDisconfirmationCritic(true) },
    );
    await controller.advanceIteration();
    const escalated = await controller.advanceIteration();

    const hypothesis = started.ledger.projection.hypotheses.get(started.hypothesisIds[0]!)!;
    expect(hypothesis.status).toBe("handed_off");
    expect(hypothesis.spawned_case_id).toBeTruthy();
    expect(escalated.hunt_status).toBe("active");

    const record = started.ledger.projection.handoffs[0]!;
    expect(record.hypothesis_id).toBe(started.hypothesisIds[0]!);

    // What an IR responder is handed: the claim, the numbers, the records, and
    // what the hunt could not see. Journaled, not written beside a ledger path.
    const caseFile = record.case_markdown!;
    expect(caseFile).toMatch(/# IR case/);
    expect(caseFile).toMatch(/h one/);
    expect(caseFile).toMatch(/2 corroborating source system\(s\)/);
    expect(caseFile).toContain(citations[0]!);
    expect(caseFile).toMatch(/## What the hunt could not see/);

    // The hunt carries on for its other hypothesis, and a handed-off one is
    // terminal, so it concludes normally once that one resolves.
    started.ledger.patch("hypothesis", started.hypothesisIds[1]!, {
      status: "parked",
      resolution_reason: "dropped",
    });
    const ended = await controller.advanceIteration();
    expect(ended.hunt_outcome).toBe("completed");
    expect(renderReport(finalized(started.ledger)[0]!)).toMatch(/## Escalated to incident response/);
  });

  it("refuses to escalate a hunch, and burns no re-prompt doing it", async () => {
    const started = await newLedger();
    const provider = new ScriptedDecisionProvider([handoff(started.hypothesisIds[0]!)]);
    const result = await controllerFor(started.ledger, [], { provider }).advanceIteration();

    expect(result.note).toMatch(/HANDOFF_IR refused: .* is active, not proven/);
    expect(started.ledger.projection.hypotheses.get(started.hypothesisIds[0]!)!.status).toBe("active");
    expect(started.ledger.projection.handoffs).toHaveLength(0);

    // Schema- and citation-valid, so it stands on the record and cost exactly
    // one call — the refusal is a controller judgement, not a bad emission.
    expect(provider.seenDigests).toHaveLength(1);
    expect(started.ledger.projection.decisions).toHaveLength(1);
    expect(started.ledger.projection.decisions[0]!.rejected_attempts).toBeUndefined();
  });
});

describe("the CHECKPOINT verb", () => {
  const raise: Decision = {
    action: "CHECKPOINT",
    rationale: "the evidence contradicts the premise of this hunt; I need a human",
  };

  it("parks the hunt under the ask policy", async () => {
    const started = await newLedger({ checkpoints: { budget_anomaly: "ask" } });
    const result = await controllerFor(started.ledger, [raise]).advanceIteration();

    expect(result.hunt_status).toBe("parked");
    const checkpoint = pendingCheckpoints(started.ledger.projection)[0]!;
    expect(checkpoint.checkpoint_class).toBe("budget_anomaly");
    expect(checkpoint.question).toMatch(/contradicts the premise/);
    expect(checkpoint.context!["raised_by"]).toBe("hunt_lead");

    await steer(started.queue, started.runId, "approve", "noted, keep going", { checkpoint_id: checkpoint.checkpoint_id });
    const resumed = await controllerFor(started.ledger, [INVESTIGATE]).advanceIteration();
    expect(resumed.hunt_status).toBe("active");
  });

  it("journals it and carries on under the auto policy", async () => {
    const started = await newLedger({ checkpoints: { budget_anomaly: "auto" } });
    const provider = new ScriptedDecisionProvider([raise, INVESTIGATE]);
    const controller = controllerFor(started.ledger, [], { provider });

    const result = await controller.advanceIteration();
    expect(result.hunt_status).toBe("active");
    expect(pendingCheckpoints(started.ledger.projection)).toHaveLength(0);
    expect(started.ledger.projection.resolutions.some((entry) => entry.actor === AUTO_ACTOR)).toBe(true);

    // The concern is not lost: the Hunt Lead is told nobody was asked, so it
    // acts on it rather than raising the same checkpoint every turn.
    await controller.advanceIteration();
    expect(
      provider.seenDigests[1]!.notes.join(" ") + provider.seenDigests[1]!.directives.join(" "),
    ).toMatch(/nobody was asked/);
  });
});

describe("the report carries the supervision", () => {
  it("names every checkpoint, who answered it, and what is still suppressed", async () => {
    const started = await newLedger({ checkpoints: { verdict_review: "auto" } });
    await steer(started.queue, started.runId, "benign", "our own scanner", { entity_key: SEED_KEY });
    started.ledger.patch("hypothesis", started.hypothesisIds[0]!, {
      status: "parked",
      resolution_reason: "dropped",
    });
    await controllerFor(started.ledger, [CONCLUDE]).advanceIteration();

    const report = finalized(started.ledger)[0]!;
    expect(report.checkpoints.map((checkpoint) => checkpoint.class)).toEqual([
      "hypothesis_approval",
      "verdict_review",
    ]);
    expect(report.checkpoints.every((checkpoint) => checkpoint.resolution?.actor === AUTO_ACTOR)).toBe(true);
    expect(report.suppressions).toEqual([{ entity_key: SEED_KEY, actor: expect.any(String) }]);

    const rendered = renderReport(report);
    expect(rendered).toMatch(/## Checkpoints/);
    expect(rendered).toMatch(/## Operator suppressions/);
  });
});

// The whole ticket in one walk: start approval, a verdict a human holds, the
// escalation, and an ending the same human signs off.
describe("a supervised hunt end to end", () => {
  it("runs start approval → parked verdict → approve → proven → handoff → conclude", async () => {
    const started: Started = await newLedger({
      checkpoints: { hypothesis_approval: "ask", verdict_review: "ask" },
    });
    const hypothesisId = started.hypothesisIds[0]!;
    const citations = provable(started.ledger, hypothesisId);

    const start = pendingCheckpoints(started.ledger.projection)[0]!;
    await steer(started.queue, started.runId, "approve", "hypotheses look right", { checkpoint_id: start.checkpoint_id });

    const decisions: ScriptedDecision[] = [
      validate(hypothesisId, citations),
      { action: "HANDOFF_IR", rationale: "escalate", target_hypothesis_id: hypothesisId },
      CONCLUDE,
    ];
    const critic = new ScriptedDisconfirmationCritic(true);

    // Each leg reopens the ledger, because that is what an operator answering a
    // checkpoint hours later actually does. Every leg's writes must land first.
    let current = started.ledger;
    const leg = async (script: ScriptedDecision[]) => {
      current = await reopen(started, current);
      return { journal: current, result: await controllerFor(current, script, { critic }).advanceIteration() };
    };

    expect((await leg(decisions)).result.hunt_status).toBe("parked");

    current = await reopen(started, current);
    const review = pendingCheckpoints(current.projection)[0]!;
    expect(review.checkpoint_class).toBe("verdict_review");
    await steer(started.queue, started.runId, "approve", "checked the payloads", { checkpoint_id: review.checkpoint_id });

    expect((await leg(decisions.slice(1))).result.note).toMatch(/handed off to incident response/);
    expect((await leg(decisions.slice(2))).result.hunt_status).toBe("parked");

    current = await reopen(started, current);
    const final = pendingCheckpoints(current.projection)[0]!;
    await steer(started.queue, started.runId, "approve", "ship it", { checkpoint_id: final.checkpoint_id });

    expect((await leg([])).result.hunt_outcome).toBe("completed");
    const replayed = (await reopen(started, current)).projection;
    expect(replayed.hypotheses.get(hypothesisId)!.status).toBe("handed_off");
    expect(replayed.resolutions).toHaveLength(3);
    expect(replayed.resolutions.every((resolution) => resolution.actor !== AUTO_ACTOR)).toBe(true);
    expect(replayed.handoffs[0]!.case_markdown).toMatch(/# IR case/);
  });
});
