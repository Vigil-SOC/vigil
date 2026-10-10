import { describe, expect, it } from "vitest";
import { DEFAULT_TERMINATION, DEFAULT_VERDICTS } from "../../workflows/hunt/config.js";
import { BASE_RATE_PROVENANCE, NULL_HYPOTHESIS, nullHypothesisFor } from "../../workflows/hunt/opening.js";
import { scoredFrontier } from "../../workflows/hunt/frontier.js";
import { unclassified } from "../../workflows/hunt/strength.js";
import { evidenceFrom } from "../../workflows/hunt/adapters.js";
import { terminationVerdict } from "../../workflows/hunt/termination.js";
import { briefOf } from "../../plugins/hunt/hooks.js";
import {
  bareEvidence,
  bookOf,
  finalized,
  gapLock,
  newLedger,
  question,
  relate,
  resolve,
  type Started,
} from "../support/hunt.js";

const loop = (overrides = {}) => newLedger({ hypothesisLoop: true, ...overrides });
const nulls = (started: Started) =>
  [...started.ledger.projection.hypotheses.values()].filter((h) => h.provenance === BASE_RATE_PROVENANCE);
const contenders = (started: Started) =>
  [...started.ledger.projection.hypotheses.values()].filter((h) => h.provenance !== BASE_RATE_PROVENANCE);

describe("the null is on the board before anything is argued", () => {
  it("seeds a benign hypothesis at base rate, and shows it in the first digest", async () => {
    const started = await loop();
    // newLedger's default hypothesis; the null is phrased against it. Pinned as a
    // literal (not nullHypothesisFor(...) recomputed) so a phrasing regression in
    // the function is caught here rather than cancelling itself out on both sides.
    const expectedNull =
      'the activity described in "a credential is used from new infrastructure" has a legitimate explanation' +
      " — expected operations, sanctioned tooling, or normal user/automation behavior — and is not adversary action";

    const [seeded] = nulls(started);
    expect(seeded!.statement).toBe(expectedNull);
    expect(seeded!.status).toBe("active");
    expect(seeded!.attack_technique).toBeNull();

    // Before the first turn, so the agent never argues without the alternative in view.
    expect(started.ledger.projection.hunt.iteration).toBe(0);
    expect(briefOf(started.ledger.projection)).toContain(expectedNull);
  });

  it("leaves a legacy hunt with only the hypotheses it was given", async () => {
    const started = await newLedger();
    expect(nulls(started)).toHaveLength(0);
    expect(started.hypothesisIds).toHaveLength(1);
  });
});

describe("nullHypothesisFor phrases the base rate against the activity under test", () => {
  it("names the first non-empty hypothesis in the benign account", () => {
    expect(nullHypothesisFor(["", "  ", "lateral movement over SMB"])).toBe(
      'the activity described in "lateral movement over SMB" has a legitimate explanation' +
        " — expected operations, sanctioned tooling, or normal user/automation behavior — and is not adversary action",
    );
  });

  it("falls back to the generic null when no hypothesis is given", () => {
    expect(nullHypothesisFor([])).toBe(NULL_HYPOTHESIS);
    expect(nullHypothesisFor(["", "   "])).toBe(NULL_HYPOTHESIS);
  });
});

// unclassified() filters on provenance === "worker", and the dispatcher once never
// set it, so the count read nothing. This goes through the path a run actually takes.
describe("the dispatcher stamps the provenance the guard reads", () => {
  it("marks a worker's own findings as worker evidence", () => {
    const records = evidenceFrom({
      results: [{ source_system: "cisco:asa", summary: "412 connections", salience: "notable", why_notable: "regular", payload: {} }],
    });
    expect(records).toHaveLength(1);
    expect(records[0]!.provenance).toBe("worker");
  });

  it("leaves an observation the agent has to rule on", async () => {
    const started = await loop({ hypotheses: ["h one"] });
    // record_evidence's path: the adapter shapes it, the book appends it.
    bookOf(started.ledger).appendEvidence(
      evidenceFrom({ results: [{ source_system: "duckdb", summary: "one row", salience: "routine", why_notable: "baseline", payload: {} }] }),
    );

    // Two active hypotheses -- one given, one null -- and the observation ruled on neither.
    expect(unclassified(started.ledger.projection)).toHaveLength(2);
  });
});

// The coverage guard that refused a lead's decision is gone; rule_evidence records
// the rulings, and what is left unruled is reported (below).
describe("one observation rules on every active hypothesis", () => {
  it("records every ruling, neither included", async () => {
    const started = await loop({ hypotheses: ["h one", "h two"] });
    const evidenceId = bareEvidence(started.ledger);
    const ids = [...started.ledger.projection.hypotheses.keys()];

    const { ruled, skipped } = bookOf(started.ledger).rule([
      { evidence_id: evidenceId, hypothesis_id: ids[0]!, relation: "supports" },
      { evidence_id: evidenceId, hypothesis_id: ids[1]!, relation: "weakens" },
      { evidence_id: evidenceId, hypothesis_id: ids[2]!, relation: "neither" },
    ]);
    expect(ruled).toBe(3);
    expect(skipped).toEqual([]);

    // Applied as links, so the ruling is on the ledger -- including "neither",
    // which is an answer and not an omission.
    const relations = started.ledger.projection.links
      .filter((link) => link.evidence_id === evidenceId)
      .map((link) => link.relation)
      .sort();
    expect(relations).toEqual(["neither", "supports", "weakens"]);
    expect(unclassified(started.ledger.projection)).toHaveLength(0);
  });

  it("skips a ruling on anything the ledger does not hold", async () => {
    const started = await loop({ hypotheses: ["h one"] });
    const evidenceId = bareEvidence(started.ledger);
    const { ruled, skipped } = bookOf(started.ledger).rule([
      { evidence_id: "ev-nope", hypothesis_id: started.hypothesisIds[0]!, relation: "supports" },
      { evidence_id: evidenceId, hypothesis_id: "h-nope", relation: "supports" },
    ]);
    expect(ruled).toBe(0);
    expect(skipped).toHaveLength(2);
    expect(started.ledger.projection.links).toHaveLength(0);
  });

  it("updates a hypothesis the evidence was not gathered for", async () => {
    const started = await loop({ hypotheses: ["h one", "h two"] });
    const evidenceId = bareEvidence(started.ledger);
    const [first, second] = [...started.ledger.projection.hypotheses.keys()];

    // Gathered for the first, and it weakens the second. Confirmation drift is
    // exactly the case where that second update never happens.
    relate(started.ledger, evidenceId, first!, "supports");
    relate(started.ledger, evidenceId, second!, "weakens");

    expect(started.ledger.projection.links.filter((link) => link.hypothesis_id === second && link.relation === "weakens")).toHaveLength(1);
  });

});

describe("the frontier is ranked by discrimination, not by interest", () => {
  // Three hypotheses so one lead can bear on all of them without splitting any.
  async function twoLeads(hypothesisLoop: boolean) {
    const started = await newLedger({ hypothesisLoop, hypotheses: ["h one", "h two", "h three"] });
    const [first, second, third] = contenders(started).map((h) => h.hypothesis_id);

    // Bears on three hypotheses, all the same way: interesting, discriminates nothing.
    const broad = bareEvidence(started.ledger, "netflow");
    for (const id of [first!, second!, third!]) relate(started.ledger, broad, id, "supports");
    const volume = question(started.ledger, "sweep every host on the subnet", { spawning_evidence_id: broad });

    // Bears on two, in opposite directions: whichever way it comes back, it splits the field.
    const sharp = bareEvidence(started.ledger, "cloudtrail");
    relate(started.ledger, sharp, first!, "supports");
    relate(started.ledger, sharp, second!, "weakens");
    const discriminating = question(started.ledger, "was the key used before the alert?", {
      spawning_evidence_id: sharp,
    });

    return { started, volume, discriminating };
  }

  it("ranks the high-volume lead first under the legacy heuristic", async () => {
    const { started, volume } = await twoLeads(false);
    expect(scoredFrontier(started.ledger.projection, 1)[0]!.question.question_id).toBe(volume);
  });

  it("ranks the low-volume high-discrimination lead first under the hypothesis loop", async () => {
    const { started, volume, discriminating } = await twoLeads(true);

    const ranked = scoredFrontier(started.ledger.projection, 1);
    expect(ranked[0]!.question.question_id).toBe(discriminating);
    // The same lead the legacy scorer put first now loses, so this is the scorer
    // changing the answer rather than the fixture arranging one.
    expect(ranked.find((entry) => entry.question.question_id === volume)!.score).toBeLessThan(ranked[0]!.score);
  });

  it("still tops out at 16, so priority_floor keeps the meaning it was calibrated against", async () => {
    const { started } = await twoLeads(true);
    for (const entry of scoredFrontier(started.ledger.projection, 1)) {
      expect(entry.score).toBeLessThanOrEqual(16);
    }
  });
});

describe("stop when one dominates, or when none can", () => {
  it("refuses to conclude while a contender is unresolved, though the null is active", async () => {
    const started = await loop();
    const verdict = terminationVerdict(started.ledger.projection, 1, DEFAULT_TERMINATION, DEFAULT_VERDICTS);

    expect(verdict.outcome).toBeNull();
    // The contender blocks, not the null -- otherwise no hunt could ever stop.
    expect(verdict.outcome === null && verdict.blocked_by).toContain(contenders(started)[0]!.hypothesis_id);
  });

  it("completes when a hypothesis dominates, with the null still on the record", async () => {
    const started = await loop();
    started.ledger.patch("hypothesis", contenders(started)[0]!.hypothesis_id, {
      status: "proven",
      resolution_reason: "survived",
    });

    expect(terminationVerdict(started.ledger.projection, 1, DEFAULT_TERMINATION, DEFAULT_VERDICTS).outcome).toBe(
      "completed",
    );
  });

  it("ends data_starved when no hypothesis can dominate, rather than guessing", async () => {
    const started = await loop();
    await gapLock(started.ledger, contenders(started)[0]!.hypothesis_id);

    const concluded = bookOf(started.ledger).conclude("nothing can dominate");

    // The honest ending the document asks for: better to fail and not know.
    expect(concluded.concluded).toBe(true);
    expect(started.ledger.projection.hunt.outcome).toBe("data_starved");
    expect(finalized(started.ledger)[0]!.outcome).toBe("data_starved");
  });

  it("reports the observations it ended without ruling on, and omits the count on a legacy run", async () => {
    const started = await loop();
    bareEvidence(started.ledger);
    resolve(started.ledger, contenders(started)[0]!.hypothesis_id, "proven");
    bookOf(started.ledger).terminate("completed");

    // A hunt that stopped mid-classification says so rather than reading as if
    // every observation had been weighed.
    expect(finalized(started.ledger)[0]!.unruled).toBe(1);

    const legacy = await newLedger();
    bareEvidence(legacy.ledger);
    bookOf(legacy.ledger).terminate("completed");
    // Absent, not zero: a legacy run was never asked to rule on anything, and a
    // new key would move the fold the #625 gate pins.
    expect(finalized(legacy.ledger)[0]!.unruled).toBeUndefined();
  });

  it("keeps the three endings distinct", async () => {
    const starved = await loop();
    await gapLock(starved.ledger, contenders(starved)[0]!.hypothesis_id);
    bookOf(starved.ledger).conclude("starved");

    const done = await loop();
    resolve(done.ledger, contenders(done)[0]!.hypothesis_id, "proven");
    bookOf(done.ledger).conclude("done");

    // "we could not look" and "we cleared it" must not read the same to an analyst.
    expect(finalized(starved.ledger)[0]!.outcome).toBe("data_starved");
    expect(finalized(done.ledger)[0]!.outcome).toBe("completed");
    expect(finalized(starved.ledger)[0]!.outcome).not.toBe(finalized(done.ledger)[0]!.outcome);
  });
});

// "refusing an entity the graph does not know" validated the lead's target_entity,
// which the decision vocabulary took with it.

// A failed dispatch is evidence about this deployment, and its text is ours. The
// extractor read IPs out of "read tcp 172.18.0.3:46528->160.79.104.10:443" -- a
// Docker bridge address and the model gateway -- and put them on the board as
// observables. A worker then spent a turn deciding whether api.anthropic.com was
// attacker infrastructure and wrote that a Frothly host had beaconed to it.
describe("a hunt does not investigate its own plumbing", () => {
  // The salvage of a dying worker's rows went with worker dispatch; salvaged() itself is in calls.test.ts.
  const GATEWAY = "Error reading stream: read tcp 172.18.0.3:46528->160.79.104.10:443: read: connection timed out";

  it("takes no entities from a tool failure, however many addresses it names", async () => {
    const started = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    const [record] = bookOf(started.ledger).appendEvidence([
      {
        source_system: "dispatcher",
        summary: GATEWAY,
        payload: { failure_reason: GATEWAY },
        salience: "routine",
        why_notable: "a query the hunt wanted could not be run",
        provenance: "tool_failure",
        attacker_influenceable: false,
        instruction_like: false,
      },
    ]);
    expect(record?.entities).toEqual([]);
  });

  // The blind spot observe() records for a failed remote call: its summary is ours,
  // never the failure text, so nothing of the plumbing reaches the board at all.
  it("records a failed call's blind spot without the failure text", async () => {
    const started = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    bookOf(started.ledger).appendGap("splunk_search", "timeout", started.hypothesisIds[0]!);

    const [record] = [...started.ledger.projection.evidence.values()];
    expect(record?.provenance).toBe("tool_failure");
    expect(record?.summary).not.toMatch(/\d+\.\d+\.\d+\.\d+/);
    expect(record?.entities).toEqual([]);
    expect(record?.payload).toMatchObject({ tool: "splunk_search", kind: "timeout" });
  });

  // The estate's own addresses still have to reach the board: this refuses a
  // source, not a shape. Same path, same kind of text, opposite provenance.
  it("still takes entities from a real finding", async () => {
    const started = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    const [record] = bookOf(started.ledger).appendEvidence(
      evidenceFrom({ results: [{ source_system: "net_flow", summary: "HOST-42 reached 45.77.53.176 every 30s", salience: "notable", why_notable: "low jitter", payload: {} }] }),
    );
    expect(record?.provenance).toBe("worker");
    expect(record?.entities.map((one) => one.value)).toContain("45.77.53.176");
  });
});
