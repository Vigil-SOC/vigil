import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { beforeEach, describe, expect, it } from "vitest";
import { drain, inboxPath, steer } from "../ai/inbox.js";
import { Lease, LeaseHeld } from "../ai/lease.js";
import { Ledger, newId, snapshots } from "../ai/ledger.js";
import { HuntController, resumeHunt, startHunt } from "../ai/loop.js";
import { ScriptedDecisionProvider, ScriptedWorkerDispatcher } from "../ai/scripted.js";
import { buildSpec } from "../ai/spec.js";
import type { Decision } from "../ai/types.js";

const INVESTIGATE: Decision = { action: "INVESTIGATE", rationale: "look", query_intent: "baseline" };
const CONCLUDE: Decision = { action: "CONCLUDE", rationale: "done" };

let dir: string;
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "hunt-"));
});

function newLedger(): Ledger {
  return startHunt(buildSpec({ prompt: "a credential is used from new infrastructure" }), dir);
}

function controllerFor(ledger: Ledger, decisions: Decision[]): HuntController {
  return new HuntController(ledger, new ScriptedDecisionProvider(decisions), new ScriptedWorkerDispatcher());
}

describe("lease", () => {
  it("blocks a second holder, is reclaimable once expired, and frees on release", () => {
    const ledger = newLedger();
    const held = Lease.acquire(ledger.path, 60_000);
    expect(() => Lease.acquire(ledger.path, 60_000)).toThrow(LeaseHeld);

    held.release();
    Lease.acquire(ledger.path, 60_000).release();

    // An expired lease is what a crashed process leaves behind; acquiring it is
    // the whole watchdog.
    Lease.acquire(ledger.path, -1);
    expect(() => Lease.acquire(ledger.path, 60_000)).not.toThrow();
  });

  it("refuses to renew a lease it no longer owns", () => {
    const ledger = newLedger();
    const stale = Lease.acquire(ledger.path, -1);
    Lease.acquire(ledger.path, 60_000);
    expect(() => stale.renew()).toThrow(LeaseHeld);
  });
});

describe("resume", () => {
  it("carries the spec in the ledger and continues from it", async () => {
    const spec = buildSpec({ prompt: "q", entity: "10.0.0.1" });
    const ledger = startHunt(spec, dir);
    await controllerFor(ledger, [INVESTIGATE, INVESTIGATE]).advanceIteration();

    const resumed = resumeHunt(ledger.path);
    expect(resumed.spec).toEqual(spec);
    expect(resumed.ledger.projection).toEqual(ledger.projection);

    const result = await controllerFor(resumed.ledger, [CONCLUDE]).advanceIteration();
    expect(result.iteration).toBe(2);
    // The controller owns termination: this CONCLUDE is refused while the
    // hypothesis is still active, so the resumed hunt simply carries on.
    expect(result.hunt_outcome).toBeNull();
  });

  it("refuses to resume a hunt that already ended", async () => {
    const ledger = newLedger();
    steer(ledger.path, "abort", "operator halted the hunt");
    await controllerFor(ledger, []).advanceIteration();
    expect(() => resumeHunt(ledger.path)).toThrow(/already ended/);
  });
});

describe("reap", () => {
  // Exactly what a crash between journaling a dispatch and recording its result
  // leaves on disk: a pending row, and a lead closed by a worker that never ran.
  function crashed(ledger: Ledger): string {
    const questionId = newId("q", 4);
    ledger.append({
      kind: "question",
      question: {
        question_id: questionId,
        question: "check 10.0.0.1",
        status: "open",
        entity_key: null,
        spawning_evidence_id: null,
        spawning_dispatch_id: null,
        spawned_iteration: 1,
        hypothesis_id: null,
      },
    });
    ledger.patch("question", questionId, { status: "closed" });
    ledger.append({
      kind: "dispatch",
      dispatch: {
        dispatch_id: newId("dsp"),
        iteration: 1,
        agent_id: "threat_hunter",
        status: "pending",
        query_intent: "baseline — check 10.0.0.1",
        target_hypothesis_id: null,
        question_id: questionId,
        failure_reason: null,
        cost_usd: 0,
        calls: [],
      },
    });
    return questionId;
  }

  it("hands an interrupted lead back and records the gap, without losing an iteration", () => {
    const ledger = newLedger();
    const questionId = crashed(ledger);
    const controller = controllerFor(ledger, []);
    const before = ledger.projection.hunt.iteration;

    expect(controller.reap()).toBe(1);
    expect([...ledger.projection.dispatches.values()].every((d) => d.status === "failed")).toBe(true);
    expect([...ledger.projection.evidence.values()].some((e) => e.provenance === "tool_failure")).toBe(true);
    expect(ledger.projection.questions.get(questionId)!.status).toBe("open");
    expect(ledger.projection.hunt.iteration).toBe(before);
  });

  it("is idempotent, so a second resume does not double-count the gap", () => {
    const ledger = newLedger();
    crashed(ledger);
    const controller = controllerFor(ledger, []);

    controller.reap();
    const evidence = ledger.projection.evidence.size;
    expect(controller.reap()).toBe(0);
    expect(ledger.projection.evidence.size).toBe(evidence);
  });

  it("leaves a completed dispatch alone", async () => {
    const ledger = newLedger();
    await controllerFor(ledger, [INVESTIGATE]).advanceIteration();
    expect([...ledger.projection.dispatches.values()].every((d) => d.status === "complete")).toBe(true);
    expect(controllerFor(ledger, []).reap()).toBe(0);
  });
});

describe("steering", () => {
  it("puts a note in the digest as direction, not as evidence", async () => {
    const ledger = newLedger();
    steer(ledger.path, "note", "pivot to DNS if this stalls");

    await controllerFor(ledger, [INVESTIGATE]).advanceIteration();
    const digest = snapshots(ledger.log)[0]!;
    expect(digest.directives).toEqual([expect.stringContaining("pivot to DNS if this stalls")]);
    expect(ledger.projection.directives).toHaveLength(1);
  });

  it("puts a lead on the frontier", async () => {
    const ledger = newLedger();
    steer(ledger.path, "lead", "check 45.77.53.176");

    await controllerFor(ledger, [INVESTIGATE]).advanceIteration();
    const questions = [...ledger.projection.questions.values()];
    expect(questions.map((q) => q.question)).toContain("check 45.77.53.176");
  });

  it("aborts before spending anything on a decision", async () => {
    const ledger = newLedger();
    steer(ledger.path, "abort", "operator halted the hunt");

    const result = await controllerFor(ledger, []).advanceIteration();
    expect(result.hunt_outcome).toBe("aborted");
    expect(result.cost_usd).toBe(0);
    expect(ledger.projection.decisions).toHaveLength(0);
    // Unresolved hypotheses are inconclusive on every terminal path, abort included.
    expect([...ledger.projection.hypotheses.values()].every((h) => h.status === "inconclusive")).toBe(true);
  });

  it("drains each directive exactly once", () => {
    const ledger = newLedger();
    steer(ledger.path, "note", "one");
    expect(drain(ledger)).toHaveLength(1);
    expect(drain(ledger)).toHaveLength(0);

    steer(ledger.path, "note", "two");
    expect(drain(ledger)).toHaveLength(1);
    expect(ledger.projection.directives.map((d) => d.text)).toEqual(["one", "two"]);
  });

  it("survives a malformed inbox line rather than killing the hunt", () => {
    const ledger = newLedger();
    writeFileSync(inboxPath(ledger.path), "{not json\n");
    expect(drain(ledger)).toEqual([]);
  });
});
