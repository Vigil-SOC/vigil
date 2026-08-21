import { describe, expect, it } from "vitest";
import { BASE_RATE_PROVENANCE, OPERATOR_HYPOTHESIS_PROVENANCE } from "../../workflows/hunt/controller.js";
import { buildDigest } from "../../workflows/hunt/digest.js";
import { terminationVerdict } from "../../workflows/hunt/termination.js";
import { DEFAULT_TERMINATION, DEFAULT_VERDICTS } from "../../workflows/hunt/config.js";
import { newLedger, type Started } from "../support/hunt.js";

const board = (started: Started) => [...started.ledger.projection.hypotheses.values()];
const byProvenance = (started: Started, provenance: string) =>
  board(started).filter((hypothesis) => hypothesis.provenance === provenance);

const ASKED = "the finance subnet shows lateral movement over SMB";

describe("what the caller asked about is on the board", () => {
  it("seeds it beside the definition's, marked as the caller's own", async () => {
    const started = await newLedger({
      hypotheses: ["a credential is used from new infrastructure"],
      operatorHypotheses: [ASKED],
    });

    const [operator] = byProvenance(started, OPERATOR_HYPOTHESIS_PROVENANCE);
    expect(operator!.statement).toBe(ASKED);
    expect(operator!.status).toBe("active");
    expect(byProvenance(started, "hunt_spec")).toHaveLength(1);
  });

  // attack_techniques is positional against the definition's hypotheses. An
  // appended one at the same index would inherit a technique written for another
  // belief, which is the whole reason the two lists are kept apart.
  it("gives it no attack technique, whatever the definition's list holds", async () => {
    const started = await newLedger({
      hypotheses: ["one", "two"],
      operatorHypotheses: [ASKED],
    });

    expect(byProvenance(started, OPERATOR_HYPOTHESIS_PROVENANCE)[0]!.attack_technique).toBeNull();
  });

  it("leaves the definition's own techniques aligned as the author wrote them", async () => {
    const started = await newLedger({ hypotheses: ["one"], operatorHypotheses: [ASKED, "another"] });

    for (const hypothesis of byProvenance(started, "hunt_spec")) {
      expect(hypothesis.statement).toBe("one");
    }
  });

  it("seeds nothing when the caller asked about nothing in particular", async () => {
    expect(byProvenance(await newLedger(), OPERATOR_HYPOTHESIS_PROVENANCE)).toEqual([]);
  });

  it("counts both in the checkpoint that starts the hunt, and says how many are the caller's", async () => {
    const started = await newLedger({ hypotheses: ["one"], operatorHypotheses: [ASKED] });

    const [checkpoint] = [...started.ledger.projection.checkpoints.values()];
    expect(checkpoint!.question).toBe("Approve and start this hunt on 2 hypothesis(es), 1 from your request?");
  });

  it("says nothing about the caller when they asked about nothing", async () => {
    const started = await newLedger({ hypotheses: ["one"] });

    const [checkpoint] = [...started.ledger.projection.checkpoints.values()];
    expect(checkpoint!.question).toBe("Approve and start this hunt on 1 hypothesis(es)?");
  });

  // A contender, not a base rate: the null must beat it like any other, so a hunt
  // cannot stop while the thing the operator actually asked about is untested.
  it("is a contender the null must beat before the hunt may stop", async () => {
    const started = await newLedger({ hypothesisLoop: true, hypotheses: [], operatorHypotheses: [ASKED] });
    const [operator] = byProvenance(started, OPERATOR_HYPOTHESIS_PROVENANCE);

    expect(byProvenance(started, BASE_RATE_PROVENANCE)).toHaveLength(1);
    const verdict = terminationVerdict(started.ledger.projection, 1, DEFAULT_TERMINATION, DEFAULT_VERDICTS);
    expect(verdict.outcome).toBeNull();
    expect((verdict as { blocked_by: string }).blocked_by).toContain(operator!.hypothesis_id);
  });
});

describe("the run's own brief reaches the lead", () => {
  const BRIEF = "**Target Case:** case-2026-0142";

  it("joins the job's brief to the playbook's standing one", async () => {
    const started = await newLedger({ narrative: "overnight traffic, nothing confirmed", prompt: BRIEF });

    expect(started.ledger.projection.hunt.narrative).toBe(
      `overnight traffic, nothing confirmed\n\n## What this run is about\n\n${BRIEF}`,
    );
  });

  // Journalled once on the run event rather than re-rendered per turn, and read
  // from there by the digest -- so a replay shows what the lead was actually told.
  it("reaches every digest the lead reads", async () => {
    const started = await newLedger({ prompt: BRIEF });

    expect(buildDigest(started.ledger.projection, 1).narrative).toContain(BRIEF);
  });

  it("leaves the narrative as the playbook wrote it when the run carried no brief", async () => {
    const started = await newLedger({ narrative: "overnight traffic, nothing confirmed" });

    expect(started.ledger.projection.hunt.narrative).toBe("overnight traffic, nothing confirmed");
  });

  it("carries the brief alone when the playbook stated no narrative", async () => {
    const started = await newLedger({ prompt: BRIEF });

    expect(started.ledger.projection.hunt.narrative).toBe(`## What this run is about\n\n${BRIEF}`);
  });
});
