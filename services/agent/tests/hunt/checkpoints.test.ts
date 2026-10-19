import { describe, expect, it } from "vitest";
import { briefOf } from "../../plugins/hunt/hooks.js";
import {
  AUTO_ACTOR,
  DEFAULT_CHECKPOINTS,
  pendingCheckpoints,
  resolutionOf,
} from "../../plugins/hunt/checkpoints.js";
import { DEFAULT_VERDICTS } from "../../plugins/hunt/config.js";
import { scoredFrontier, suppressedEntities } from "../../plugins/hunt/frontier.js";
import { steer } from "../../plugins/hunt/inbox.js";
import type { Journal } from "../../plugins/hunt/journal.js";
import { renderReport } from "../../plugins/hunt/report.js";
import { evidenceStrength, openGaps } from "../../plugins/hunt/strength.js";
import {
  bookOf,
  evidenceOn,
  finalized,
  newLedger,
  provable,
  question,
  reopen,
  SEED_IP,
  validate,
  type Started,
} from "../support/hunt.js";

const SEED_KEY = `${SEED_IP.type}:${SEED_IP.value}`;
const seen = (ledger: Journal) => ledger.projection.directives.map((directive) => directive.text).join(" ");

// What the hunt plugin's before hook does at every boundary: operator input
// first, then answers that reached the ledger some other way.
async function boundary(ledger: Journal) {
  const book = bookOf(ledger);
  await book.applyDirectives();
  book.reconcile();
  await ledger.flush();
  return book;
}

describe("the ledger is the authority, the console is delivery", () => {
  it("journals an auto resolution rather than silently skipping the checkpoint", async () => {
    const { ledger, hypothesisIds } = await newLedger({ checkpoints: { verdict_review: "auto" } });
    provable(ledger, hypothesisIds[0]!);

    validate(ledger, hypothesisIds[0]!);

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
    // Nothing the hunt journals for itself may look like operator input: the
    // drain counts operator directives to know what it has already taken.
    const { ledger, hypothesisIds } = await newLedger({ checkpoints: { verdict_review: "ask" } });
    provable(ledger, hypothesisIds[0]!);

    validate(ledger, hypothesisIds[0]!);

    expect(ledger.projection.directives.filter((directive) => directive.origin !== "controller")).toHaveLength(0);
    expect(pendingCheckpoints(ledger.projection)).toHaveLength(1);
  });
});

describe("verdict review", () => {
  async function parkedOnVerdict() {
    const started = await newLedger({ checkpoints: { verdict_review: "ask" } });
    const hypothesisId = started.hypothesisIds[0]!;
    provable(started.ledger, hypothesisId);
    const result = validate(started.ledger, hypothesisId);
    await started.ledger.flush();

    expect(result.parked).toBeDefined();
    expect(started.ledger.projection.hunt.status).toBe("parked");
    return {
      started,
      hypothesisId,
      checkpointId: pendingCheckpoints(started.ledger.projection)[0]!.checkpoint_id,
    };
  }

  it("parks instead of proving, and says what it waits on", async () => {
    const { started, hypothesisId } = await parkedOnVerdict();

    expect(started.ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
    expect(started.ledger.projection.hunt.outcome).toBeNull();

    // Unanswered, a boundary changes nothing: the hunt stays parked.
    await boundary(started.ledger);
    expect(started.ledger.projection.hunt.status).toBe("parked");

    // Names the question and nothing internal: no hunt id, no directive syntax.
    const reason = bookOf(started.ledger).suspendedBecause();
    expect(reason).toMatch(/^Waiting for your approval: Mark h-\w+ proven\?/);
    expect(reason).not.toContain(started.ledger.projection.hunt.hunt_id);
    expect(reason).not.toMatch(/directive/);
  });

  it("survives process death: the ledger alone still shows it pending", async () => {
    const { started, checkpointId } = await parkedOnVerdict();

    // Nothing of this process carries over — the ledger is the whole state.
    const reopened = await reopen(started);
    expect(pendingCheckpoints(reopened.projection).map((c) => c.checkpoint_id)).toEqual([checkpointId]);
    expect(reopened.projection.hunt.status).toBe("parked");
    expect(reopened.projection.hunt.parked_reason).toContain(checkpointId);
  });

  it("applies the patch the verdict computed, with the strength snapshot from then", async () => {
    const { started, hypothesisId, checkpointId } = await parkedOnVerdict();
    const atValidateTime = [...started.ledger.projection.checkpoints.values()][1]!.context![
      "evidence_strength"
    ] as Record<string, unknown>;

    await steer(started.queue, started.runId, "approve", "reviewed the payloads, this holds", { checkpoint_id: checkpointId });
    const resumed = await reopen(started);
    await boundary(resumed);

    // The snapshot is the one the reviewer was shown, never one recomputed on
    // approval: the same verdict delivered late, not a better-informed one.
    const hypothesis = resumed.projection.hypotheses.get(hypothesisId)!;
    expect(hypothesis.status).toBe("proven");
    expect(hypothesis.evidence_strength).toEqual(atValidateTime);
    expect(hypothesis.evidence_strength!.corroborating_sources).toBe(2);
    expect(resumed.projection.hunt.status).toBe("active");
  });

  it("refuses to land a verdict the argue-the-null pass no longer covers", async () => {
    const { started, hypothesisId, checkpointId } = await parkedOnVerdict();

    // Support the critic never argued against arrives while the review waits, so
    // the stored patch's "survived disconfirmation" is no longer true by landing.
    evidenceOn(started.ledger, hypothesisId, { source: "okta" });
    expect(evidenceStrength(started.ledger.projection, hypothesisId).survived_disconfirmation).toBe(false);

    await steer(started.queue, started.runId, "approve", "reviewed the payloads, this holds", { checkpoint_id: checkpointId });
    const resumed = await reopen(started);
    await boundary(resumed);

    expect(resumed.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
    expect(seen(resumed)).toMatch(/evidence moved while it waited.*stays active/s);
  });

  it("keeps its earned status when the operator declares a gap and then approves", async () => {
    const { started, hypothesisId, checkpointId } = await parkedOnVerdict();

    // A reviewer says the hunt is blind somewhere and approves in the same breath.
    // The gap they just declared must not be the one thing the approval ignores.
    for (const blind of ["no EDR on that subnet", "no CloudTrail before August", "netflow sampled at 1:100"]) {
      await steer(started.queue, started.runId, "gap", blind, { hypothesis_id: hypothesisId });
    }
    await steer(started.queue, started.runId, "approve", "looks right to me", { checkpoint_id: checkpointId });

    const resumed = await reopen(started);
    await boundary(resumed);

    const hypothesis = resumed.projection.hypotheses.get(hypothesisId)!;
    expect(hypothesis.status).toBe("active");
    expect(hypothesis.resolution_reason).toBe("could not finish: 3 sources unreachable");
    // The numbers on the record are the ones that stopped it, not the ones the
    // reviewer was shown — a verdict nobody can re-read is not auditable.
    expect(hypothesis.evidence_strength!.open_gaps).toBe(3);
  });

  it("leaves the hypothesis active on a rejection, with the reason on the record", async () => {
    const { started, hypothesisId, checkpointId } = await parkedOnVerdict();
    await steer(started.queue, started.runId, "reject", "the second source is the same collector under another name", {
      checkpoint_id: checkpointId,
    });

    await boundary(started.ledger);

    expect(started.ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
    expect(started.ledger.projection.hunt.status).toBe("active");
    expect(seen(started.ledger)).toMatch(/same collector under another name/);
  });

  // The agent reads an operator's reasons off its brief, as the lead read them off its digest.
  it("tells the agent why its verdict was rejected", async () => {
    const { started, checkpointId } = await parkedOnVerdict();
    await steer(started.queue, started.runId, "reject", "the second source is the same collector under another name", {
      checkpoint_id: checkpointId,
    });

    await boundary(started.ledger);

    expect(briefOf(started.ledger.projection)).toMatch(/same collector under another name/);
  });

  it("answers a checkpoint by id, so a stale or duplicate answer changes nothing", async () => {
    const { started, hypothesisId, checkpointId } = await parkedOnVerdict();
    await steer(started.queue, started.runId, "reject", "not yet", { checkpoint_id: checkpointId });
    await steer(started.queue, started.runId, "approve", "changed my mind", { checkpoint_id: checkpointId });

    await boundary(started.ledger);

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

    const parked = bookOf(started.ledger).conclude("nothing further to run");
    expect(parked.concluded).toBe(false);
    expect(parked.parked).toBeDefined();
    expect(started.ledger.projection.hunt.status).toBe("parked");
    expect(started.ledger.projection.hunt.outcome).toBeNull();

    const checkpointId = pendingCheckpoints(started.ledger.projection)[0]!.checkpoint_id;
    await steer(started.queue, started.runId, "approve", "agreed, we are done", { checkpoint_id: checkpointId });
    await boundary(started.ledger);

    expect(started.ledger.projection.hunt.outcome).toBe("completed");
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
    bookOf(started.ledger).conclude("nothing further to run");

    const checkpointId = pendingCheckpoints(started.ledger.projection)[0]!.checkpoint_id;
    await steer(started.queue, started.runId, "reject", "check the second host first", { checkpoint_id: checkpointId });
    await boundary(started.ledger);

    expect(started.ledger.projection.hunt.status).toBe("active");
    expect(started.ledger.projection.hunt.outcome).toBeNull();
    expect(finalized(started.ledger)).toHaveLength(0);
  });
});

describe("the start approval", () => {
  it("holds the hunt at pending_approval until it is journaled", async () => {
    const started = await newLedger({ checkpoints: { hypothesis_approval: "ask" } });

    expect(started.ledger.projection.hunt.status).toBe("pending_approval");
    expect(pendingCheckpoints(started.ledger.projection)[0]!.checkpoint_class).toBe("hypothesis_approval");
    await boundary(started.ledger);
    expect(started.ledger.projection.hunt.status).toBe("pending_approval");
    expect(bookOf(started.ledger).suspendedBecause()).toMatch(/^Waiting for your approval: Approve and start this hunt/);

    const checkpointId = pendingCheckpoints(started.ledger.projection)[0]!.checkpoint_id;
    await steer(started.queue, started.runId, "approve", "reviewed the hypotheses", { checkpoint_id: checkpointId });
    await boundary(started.ledger);

    expect(started.ledger.projection.hunt.status).toBe("active");
    expect(resolutionOf(started.ledger.projection, checkpointId)!.actor).not.toBe(AUTO_ACTOR);
  });

  it("aborts a rejected start through terminate(), so it still finalizes", async () => {
    const started = await newLedger({ checkpoints: { hypothesis_approval: "ask" } });
    const checkpointId = pendingCheckpoints(started.ledger.projection)[0]!.checkpoint_id;
    await steer(started.queue, started.runId, "reject", "wrong scope, start again", { checkpoint_id: checkpointId });

    await boundary(started.ledger);

    expect(started.ledger.projection.hunt.outcome).toBe("aborted");
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
  // Known-benign used to bind the lead by refusing its INVESTIGATE/DEEPEN/PIVOT on
  // the entity; there is no decision to validate now. What the agent is told is below.

  // A known-benign entity reaches the agent's brief, so it opens no new work on it.
  it("tells the agent an entity is known-benign", async () => {
    const started = await newLedger();
    await steer(started.queue, started.runId, "benign", "our own scanner", { entity_key: SEED_KEY });

    await boundary(started.ledger);

    expect(briefOf(started.ledger.projection)).toMatch(/known-benign/);
  });

  it("suppresses an entity without touching a single record, and lets a revoke lift it", async () => {
    const started = await newLedger();
    const evidenceId = evidenceOn(started.ledger, started.hypothesisIds[0]!, {
      source: "duckdb",
      entities: [SEED_IP],
    });
    const before = started.ledger.projection.evidence.get(evidenceId)!;

    await steer(started.queue, started.runId, "benign", "our own scanner", { entity_key: SEED_KEY });
    await boundary(started.ledger);

    expect([...suppressedEntities(started.ledger.projection).keys()]).toEqual([SEED_KEY]);
    // The evidence is exactly what it was: a suppression is an authorization,
    // not a deletion.
    expect(started.ledger.projection.evidence.get(evidenceId)).toEqual(before);
    expect(seen(started.ledger)).toMatch(/known-benign/);

    await steer(started.queue, started.runId, "benign", "put it back in play", { entity_key: SEED_KEY, revoke: true });
    await boundary(started.ledger);

    expect(suppressedEntities(started.ledger.projection).size).toBe(0);
    expect(seen(started.ledger)).toMatch(/lifted the known-benign suppression/);
  });

  // Enrichment and pivot candidates were the controller's own fan-out; the agent
  // enriches through its tools, so there is no controller enrichment to keep it out of.

  it("counts an operator-declared gap like a tool failure, up to gap-lock", async () => {
    const started = await newLedger();
    const hypothesisId = started.hypothesisIds[0]!;
    provable(started.ledger, hypothesisId);

    for (const text of ["no EDR on the 10.30.0.0/16 subnet", "no DNS logging before 03:00", "no proxy logs at all"]) {
      await steer(started.queue, started.runId, "gap", text, { hypothesis_id: hypothesisId });
    }
    await boundary(started.ledger);

    expect(openGaps(started.ledger.projection, hypothesisId)).toBe(DEFAULT_VERDICTS.gap_lock_threshold);

    // Enough blindness that the verdict is not given: the hunt could not look,
    // which is never the same as having cleared it, and it is not a ruling either.
    const result = validate(started.ledger, hypothesisId);

    expect(started.ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
    expect(result.said).toMatch(/gap-locked/);
  });

  it("pins a boosted question to the top of the frontier", async () => {
    const started = await newLedger();
    question(started.ledger, "the obvious next thread", { entity_key: "ip:10.0.0.1" });
    // Stale enough to rank below it on recency, so the pin is doing the work
    // rather than a tie-break.
    const buried = question(started.ledger, "a thread nobody ranked", { spawned_iteration: -5 });

    expect(scoredFrontier(started.ledger.projection, 1)[0]!.question.question_id).not.toBe(buried);

    await steer(started.queue, started.runId, "boost", "look at this one next", { question_id: buried });
    await boundary(started.ledger);

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

    await boundary(started.ledger);

    expect(seen(started.ledger)).toMatch(/backup window/);
  });

  // A correction to the hunt's premise is in front of the agent at its next pick-up.
  it("puts a premise correction in front of the agent", async () => {
    const started = await newLedger();
    await steer(started.queue, started.runId, "note", "the 03:00 spike is our backup window, not exfil");

    await boundary(started.ledger);

    expect(briefOf(started.ledger.projection)).toMatch(/backup window/);
  });
});

// A hard abort preempting queued workers went with the workers: the agent makes
// one call at a time, and the kernel's stop watch halts it mid-turn.

describe("scope", () => {
  it("refuses a cross-tenant lead outright rather than raising a checkpoint", async () => {
    const started = await newLedger({ checkpoints: { scope_extension: "ask" }, scope: { tenant: "frothly" } });
    await steer(started.queue, started.runId, "lead", "check tenant:acme for the same key");

    await boundary(started.ledger);

    // No checkpoint: a tenant boundary is not one an operator may waive from
    // inside the hunt, so there is nothing to approve.
    expect(pendingCheckpoints(started.ledger.projection)).toHaveLength(0);
    expect(started.ledger.projection.hunt.status).toBe("active");
    expect([...started.ledger.projection.questions.values()]).toHaveLength(0);
    expect(seen(started.ledger)).toMatch(/names tenant acme.*Refused/s);
  });

  it("asks before growing past the declared scope, and grows on approval", async () => {
    const started = await newLedger({
      checkpoints: { scope_extension: "ask" },
      scope: { tenant: "frothly", entities: ["ip:10.0.0.5"] },
    });
    await steer(started.queue, started.runId, "lead", `pull on ${SEED_IP.value} as well`);

    await boundary(started.ledger);
    expect(started.ledger.projection.hunt.status).toBe("parked");
    const checkpoint = pendingCheckpoints(started.ledger.projection)[0]!;
    expect(checkpoint.checkpoint_class).toBe("scope_extension");
    expect(checkpoint.question).toContain(SEED_KEY);
    expect([...started.ledger.projection.questions.values()]).toHaveLength(0);

    await steer(started.queue, started.runId, "approve", "yes, it is ours", { checkpoint_id: checkpoint.checkpoint_id });
    await boundary(started.ledger);

    expect(started.ledger.projection.hunt.status).toBe("active");
    expect([...started.ledger.projection.questions.values()].map((entry) => entry.question)).toEqual([
      `pull on ${SEED_IP.value} as well`,
    ]);
    expect(started.ledger.projection.hunt.scope["entities"]).toEqual(["ip:10.0.0.5", SEED_KEY]);
  });

  it("leaves a hunt that declared no scope free of scope checkpoints", async () => {
    const started = await newLedger({ checkpoints: { scope_extension: "ask" } });
    await steer(started.queue, started.runId, "lead", `check ${SEED_IP.value}`);

    await boundary(started.ledger);

    expect(started.ledger.projection.hunt.status).toBe("active");
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

    await boundary(started.ledger);

    expect(started.ledger.projection.hunt.status).toBe("active");
    expect(pendingCheckpoints(started.ledger.projection)).toHaveLength(0);
  });
});

describe("handoff to incident response", () => {
  it("escalates a proven hypothesis, journals the case file, and keeps hunting", async () => {
    const started = await newLedger({
      checkpoints: { verdict_review: "auto" },
      hypotheses: ["h one", "h two"],
    });
    const citations = provable(started.ledger, started.hypothesisIds[0]!);
    const book = bookOf(started.ledger);

    validate(started.ledger, started.hypothesisIds[0]!);
    const escalated = book.handoff(started.hypothesisIds[0]!, "the key is being used right now; IR should rotate it");

    expect(escalated.refused).toBe(false);
    const hypothesis = started.ledger.projection.hypotheses.get(started.hypothesisIds[0]!)!;
    expect(hypothesis.status).toBe("handed_off");
    expect(hypothesis.spawned_case_id).toBeTruthy();
    expect(started.ledger.projection.hunt.status).toBe("active");

    const record = started.ledger.projection.handoffs[0]!;
    expect(record.hypothesis_id).toBe(started.hypothesisIds[0]!);

    // What an IR responder is handed: the claim, the numbers, the records, and
    // what the hunt could not see. Journaled, not written beside a ledger path.
    const caseFile = record.case_markdown!;
    expect(caseFile).toMatch(/# Handoff — /);
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
    expect(book.conclude("done").concluded).toBe(true);
    expect(started.ledger.projection.hunt.outcome).toBe("completed");
    expect(renderReport(finalized(started.ledger)[0]!)).toMatch(/## Escalated to incident response/);
  });

  it("refuses to escalate a hunch", async () => {
    const started = await newLedger();
    const result = bookOf(started.ledger).handoff(started.hypothesisIds[0]!, "the key is being used right now");

    expect(result.refused).toBe(true);
    expect(result.said).toMatch(/refused: .* is active, not proven/);
    expect(started.ledger.projection.hypotheses.get(started.hypothesisIds[0]!)!.status).toBe("active");
    expect(started.ledger.projection.handoffs).toHaveLength(0);
  });
});

describe("asking the operator", () => {
  const concern = "the evidence contradicts the premise of this hunt; I need a human";

  it("parks the hunt under the ask policy", async () => {
    const started = await newLedger({ checkpoints: { budget_anomaly: "ask" } });
    const result = bookOf(started.ledger).askOperator(concern, null);

    expect(result.parked).toBeDefined();
    expect(started.ledger.projection.hunt.status).toBe("parked");
    const checkpoint = pendingCheckpoints(started.ledger.projection)[0]!;
    expect(checkpoint.checkpoint_class).toBe("budget_anomaly");
    expect(checkpoint.question).toMatch(/contradicts the premise/);
    expect(checkpoint.context!["raised_by"]).toBe("hunt_agent");

    await steer(started.queue, started.runId, "approve", "noted, keep going", { checkpoint_id: checkpoint.checkpoint_id });
    await boundary(started.ledger);
    expect(started.ledger.projection.hunt.status).toBe("active");
  });

  it("journals it and carries on under the auto policy", async () => {
    const started = await newLedger({ checkpoints: { budget_anomaly: "auto" } });

    const result = bookOf(started.ledger).askOperator(concern, null);

    expect(result.parked).toBeUndefined();
    expect(started.ledger.projection.hunt.status).toBe("active");
    expect(pendingCheckpoints(started.ledger.projection)).toHaveLength(0);
    expect(started.ledger.projection.resolutions.some((entry) => entry.actor === AUTO_ACTOR)).toBe(true);
    // The concern is not lost: the agent is told nobody was asked, so it acts on
    // it rather than raising the same checkpoint every turn.
    expect(result.said).toMatch(/policy is auto/);
    expect(seen(started.ledger)).toMatch(/nobody was asked/);
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
    const book = await boundary(started.ledger);
    book.conclude("nothing further to run");

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
    provable(started.ledger, hypothesisId);

    const start = pendingCheckpoints(started.ledger.projection)[0]!;
    await steer(started.queue, started.runId, "approve", "hypotheses look right", { checkpoint_id: start.checkpoint_id });

    // Each leg reopens the ledger, because that is what an operator answering a
    // checkpoint hours later actually does. Every leg's writes must land first.
    let current = started.ledger;
    const leg = async (act: (book: ReturnType<typeof bookOf>) => unknown) => {
      current = await reopen(started, current);
      const book = await boundary(current);
      const result = book.ended() ? null : act(book);
      await current.flush();
      return { book, result };
    };

    expect((await leg((book) => validate(book.ledger, hypothesisId))).book.hunt.status).toBe("parked");

    current = await reopen(started, current);
    const review = pendingCheckpoints(current.projection)[0]!;
    expect(review.checkpoint_class).toBe("verdict_review");
    await steer(started.queue, started.runId, "approve", "checked the payloads", { checkpoint_id: review.checkpoint_id });

    const handed = await leg((book) => book.handoff(hypothesisId, "escalate"));
    expect((handed.result as { said: string }).said).toMatch(/handed off to incident response/);
    expect((await leg((book) => book.conclude("done"))).book.hunt.status).toBe("parked");

    current = await reopen(started, current);
    const final = pendingCheckpoints(current.projection)[0]!;
    await steer(started.queue, started.runId, "approve", "ship it", { checkpoint_id: final.checkpoint_id });

    expect((await leg(() => null)).book.hunt.outcome).toBe("completed");
    const replayed = (await reopen(started, current)).projection;
    expect(replayed.hypotheses.get(hypothesisId)!.status).toBe("handed_off");
    expect(replayed.resolutions).toHaveLength(3);
    expect(replayed.resolutions.every((resolution) => resolution.actor !== AUTO_ACTOR)).toBe(true);
    expect(replayed.handoffs[0]!.case_markdown).toMatch(/# Handoff — /);
  });
});
