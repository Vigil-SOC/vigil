import { describe, expect, it } from "vitest";
import { archFor } from "../../arch/registry.js";
import type { AgentEvent } from "../../contracts/events.js";
import { AUTO_ACTOR, raiseCheckpoint, resolveCheckpoint } from "../../workflows/hunt/checkpoints.js";
import { EVIDENCE_SHOWN, huntProjection } from "../../workflows/hunt/projection.js";
import { evidenceOn, newLedger, type Started } from "../support/hunt.js";

async function project(started: Started) {
  await started.ledger.flush();
  return huntProjection(started.runId, await started.state.read(started.runId));
}

// A hunt has no steps to report progress against, so a reader outside this process
// is told what it has tested and how each belief stands.
describe("what a reader is told about a hunt in flight", () => {
  it("names every hypothesis and where it stands", async () => {
    const started = await newLedger();
    const view = await project(started);

    expect(view.hypotheses.map((one) => one.hypothesis_id)).toEqual(started.hypothesisIds);
    expect(view.hypotheses.every((one) => one.statement.trim() !== "")).toBe(true);
    expect(view.status).toBe("active");
  });

  it("counts the evidence the run has gathered", async () => {
    const started = await newLedger();
    evidenceOn(started.ledger, started.hypothesisIds[0] as string);
    evidenceOn(started.ledger, started.hypothesisIds[0] as string);

    expect((await project(started)).evidence_count).toBe(2);
  });

  // The only question a supervisor actually asks of a parked run. Reported from
  // the ledger rather than a flag, so a checkpoint already answered is not re-asked.
  it("reports the checkpoint a resolution has to answer", async () => {
    const started = await newLedger();
    const raised = raiseCheckpoint("scope_extension", 1, "widen to the DMZ?");
    started.ledger.append({ kind: "checkpoint", payload: raised });
    const view = await project(started);

    expect(view.open_checkpoint?.checkpoint_class).toBe("scope_extension");
    expect(view.open_checkpoint?.question).toBe("widen to the DMZ?");
  });

  it("reports nothing open once the checkpoint is answered", async () => {
    const started = await newLedger();
    const raised = raiseCheckpoint("scope_extension", 1, "widen?");
    started.ledger.append({ kind: "checkpoint", payload: raised });
    started.ledger.append({ kind: "resolution", payload: resolveCheckpoint(raised, "approve", AUTO_ACTOR, "yes") });

    expect((await project(started)).open_checkpoint).toBeNull();
  });
});

// The registry entry is what serve.ts reads: a hunt whose projection is unregistered
// answers 404 no matter how well the fold works.
it("is what the hunt's arch entry hands a reader", async () => {
  const started = await newLedger();
  await started.ledger.flush();
  const events = await started.state.read(started.runId);
  // Erased the way serve.ts hands them over: it reads an untyped ledger and the
  // entry is what knows the kind.
  const erased = events as unknown as readonly AgentEvent<Record<never, never>>[];

  expect(archFor("hunt").projection?.(started.runId, erased)).toEqual(huntProjection(started.runId, events));
});

// A count is not a finding. The projection reported evidence_count and nothing
// else, so a console watching a run could say "4 pieces of evidence gathered" and
// never what any of them said -- for the whole of the run, which is exactly when
// somebody is watching. The records were in the fold the entire time.
describe("what the evidence actually says", () => {

  it("reports each record, not only how many there are", async () => {
    const started = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    evidenceOn(started.ledger, started.hypothesisIds[0] as string, { source: "splunk" });

    const [record] = (await project(started)).evidence;
    expect(record?.source_system).toBe("splunk");
    expect(record?.summary).toMatch(/authenticate/);
    expect(record?.why_notable).toBe("first use of this ASN by the identity");
    expect(record?.salience).toBe("notable");
  });

  // Which belief it bears on and how is the point of a piece of evidence; a record
  // linked to nothing is the case most worth seeing.
  it("says which beliefs it bears on and in which direction", async () => {
    const started = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    const hypothesisId = started.hypothesisIds[0] as string;
    evidenceOn(started.ledger, hypothesisId, { relation: "weakens" });

    expect((await project(started)).evidence[0]?.bears_on).toEqual([
      { hypothesis_id: hypothesisId, relation: "weakens" },
    ]);
  });

  // The flags the verdict gate reads, so a reader can see why support did not carry.
  it("carries the flags that stop a record counting on its own", async () => {
    const started = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    evidenceOn(started.ledger, started.hypothesisIds[0] as string, { attackerInfluenceable: true });

    expect((await project(started)).evidence[0]?.attacker_influenceable).toBe(true);
    expect((await project(started)).evidence[0]?.is_gap).toBe(false);
  });

  it("shows the newest first, so a poll opens on what just happened", async () => {
    const started = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    const hypothesisId = started.hypothesisIds[0] as string;
    evidenceOn(started.ledger, hypothesisId, { source: "older" });
    evidenceOn(started.ledger, hypothesisId, { source: "newer" });

    const sources = (await project(started)).evidence.map((one) => one.source_system);
    expect(sources.indexOf("newer")).toBeLessThan(sources.indexOf("older"));
  });

  // Capped so a five-second poll does not carry a whole run's transcript, and
  // evidence_count stays the untruncated total so a reader can say it was capped.
  it("caps the records it ships and still counts them all", async () => {
    const started = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    const hypothesisId = started.hypothesisIds[0] as string;
    for (let n = 0; n < EVIDENCE_SHOWN + 3; n += 1) evidenceOn(started.ledger, hypothesisId);

    const projection = await project(started);
    expect(projection.evidence).toHaveLength(EVIDENCE_SHOWN);
    expect(projection.evidence_count).toBe(EVIDENCE_SHOWN + 3);
  });
});

// The standings say what a hunt believes; nothing said how it got there. An operator
// watching a run could see the turn counter move and not which move it made.
describe("the moves the lead made", () => {
  it("carries each decision, newest first, without the digest it was made over", async () => {
    const started = await newLedger();
    started.ledger.append({
      kind: "decision",
      payload: {
        decision: { action: "INVESTIGATE", rationale: "start broad", query_intent: "who did it talk to" },
        decision_id: "dec-1",
        iteration: 1,
        model_id: "m",
        prompt_version: "v1",
        cost_usd: 0.01,
        digest_presented: { iteration: 1 },
        created_at: new Date().toISOString(),
      } as never,
    });
    started.ledger.append({
      kind: "decision",
      payload: {
        decision: { action: "VALIDATE", rationale: "worth testing", target_hypothesis_id: started.hypothesisIds[0] },
        decision_id: "dec-2",
        iteration: 2,
        model_id: "m",
        prompt_version: "v1",
        cost_usd: 0.02,
        rejected_attempts: ["VALIDATE must cite the evidence it rests on"],
        digest_presented: { iteration: 2 },
        created_at: new Date().toISOString(),
      } as never,
    });
    const view = await project(started);

    expect(view.moves.map((move) => move.action)).toEqual(["VALIDATE", "INVESTIGATE"]);
    expect(view.moves[0]!.rejected_attempts).toHaveLength(1);
    expect(JSON.stringify(view.moves)).not.toContain("digest_presented");
  });
});
