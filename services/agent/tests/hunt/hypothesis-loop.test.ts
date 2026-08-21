import { describe, expect, it } from "vitest";
import { DEFAULT_TERMINATION, DEFAULT_VERDICTS } from "../../workflows/hunt/config.js";
import {
  BASE_RATE_PROVENANCE,
  InvalidDecision,
  NULL_HYPOTHESIS,
  validateDecision,
} from "../../workflows/hunt/controller.js";
import { buildDigest, scoredFrontier } from "../../workflows/hunt/digest.js";
import { unclassified } from "../../workflows/hunt/strength.js";
import { evidenceFrom, salvaged } from "../../workflows/hunt/adapters.js";
import { terminationVerdict } from "../../workflows/hunt/termination.js";
import type { Decision } from "../../workflows/hunt/types.js";
import {
  bareEvidence,
  CONCLUDE,
  evidenceOn,
  controllerFor,
  finalized,
  gapLock,
  INVESTIGATE,
  newLedger,
  question,
  relate,
  resolve,
  ruled,
  SEED_IP,
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

    const [seeded] = nulls(started);
    expect(seeded!.statement).toBe(NULL_HYPOTHESIS);
    expect(seeded!.status).toBe("active");
    expect(seeded!.attack_technique).toBeNull();

    // Before iteration 1, so the lead never argues without the alternative in view.
    expect(started.ledger.projection.hunt.iteration).toBe(0);
    const digest = buildDigest(started.ledger.projection, 1);
    expect(digest.hypotheses.map((h) => h.statement)).toContain(NULL_HYPOTHESIS);
  });

  it("leaves a legacy hunt with only the hypotheses it was given", async () => {
    const started = await newLedger();
    expect(nulls(started)).toHaveLength(0);
    expect(started.hypothesisIds).toHaveLength(1);
  });
});

// The guard above was dead in production for as long as it has existed: every test
// of it builds its evidence by hand and sets provenance itself, while the real
// dispatcher never set it at all. unclassified() filters on provenance === "worker",
// so it always returned nothing and validateCoverage never rejected a decision.
// This is the test that goes through the path a run actually takes.
describe("the dispatcher stamps the provenance the guard reads", () => {
  it("marks a worker's own findings as worker evidence", () => {
    const records = evidenceFrom({
      results: [{ source_system: "cisco:asa", summary: "412 connections", salience: "notable", why_notable: "regular", payload: {} }],
    });
    expect(records).toHaveLength(1);
    expect(records[0]!.provenance).toBe("worker");
  });

  it("leaves an observation the lead has to rule on", async () => {
    const started = await loop({ hypotheses: ["h one"] });
    const [record] = evidenceFrom({
      results: [{ source_system: "duckdb", summary: "one row", salience: "routine", why_notable: "baseline", payload: {} }],
    });
    const controller = controllerFor(started.ledger, [INVESTIGATE], {
      dispatcher: {
        dispatch: async (request: { dispatch_id: string }) => ({
          dispatch_id: request.dispatch_id,
          evidence: [record!],
          failed: false,
          failure_reason: "",
          cost_usd: 0,
        }),
      } as never,
    });

    await controller.advanceIteration();

    // Two active hypotheses -- one given, one null -- and the observation ruled on
    // neither, which is precisely what the drift guard exists to refuse.
    expect(unclassified(started.ledger.projection)).toHaveLength(2);
  });
});

describe("one observation rules on every active hypothesis", () => {
  it("refuses a decision that leaves an observation unruled", async () => {
    const started = await loop({ hypotheses: ["h one", "h two"] });
    bareEvidence(started.ledger);

    // Three active hypotheses -- two given, one null -- and nothing ruled on.
    expect(unclassified(started.ledger.projection)).toHaveLength(3);
    expect(() => validateDecision(INVESTIGATE, started.ledger.projection)).toThrow(InvalidDecision);
    expect(() => validateDecision(INVESTIGATE, started.ledger.projection)).toThrow(/leaves 3 observation\(s\) unruled/);
  });

  it("accepts the decision once every hypothesis has been ruled on, neither included", async () => {
    const started = await loop({ hypotheses: ["h one", "h two"] });
    const evidenceId = bareEvidence(started.ledger);
    const ids = [...started.ledger.projection.hypotheses.keys()];

    const complete: Decision = {
      ...INVESTIGATE,
      evidence_relations: [
        { evidence_id: evidenceId, hypothesis_id: ids[0]!, relation: "supports" },
        { evidence_id: evidenceId, hypothesis_id: ids[1]!, relation: "weakens" },
        { evidence_id: evidenceId, hypothesis_id: ids[2]!, relation: "neither" },
      ],
    };
    expect(() => validateDecision(complete, started.ledger.projection)).not.toThrow();

    await controllerFor(started.ledger, [complete]).advanceIteration();

    // Applied as links, so the ruling is on the ledger rather than only in the
    // decision -- including "neither", which is an answer and not an omission.
    const relations = started.ledger.projection.links
      .filter((link) => link.evidence_id === evidenceId)
      .map((link) => link.relation)
      .sort();
    expect(relations).toEqual(["neither", "supports", "weakens"]);
    expect(unclassified(started.ledger.projection)).toHaveLength(0);
  });

  it("updates a hypothesis the evidence was not gathered for", async () => {
    const started = await loop({ hypotheses: ["h one", "h two"] });
    const evidenceId = bareEvidence(started.ledger);
    const [first, second] = [...started.ledger.projection.hypotheses.keys()];

    // Gathered for the first, and it weakens the second. Confirmation drift is
    // exactly the case where that second update never happens.
    relate(started.ledger, evidenceId, first!, "supports");
    relate(started.ledger, evidenceId, second!, "weakens");

    expect(buildDigest(started.ledger.projection, 1).weakens[second!]).toHaveLength(1);
  });

  it("does not ask a legacy hunt to rule on anything", async () => {
    const started = await newLedger();
    bareEvidence(started.ledger);
    expect(() => validateDecision(INVESTIGATE, started.ledger.projection)).not.toThrow();
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

    const result = await controllerFor(started.ledger, [ruled(started.ledger, CONCLUDE)]).advanceIteration();

    // The honest ending the document asks for: better to fail and not know.
    expect(result.hunt_outcome).toBe("data_starved");
    expect(finalized(started.ledger)[0]!.outcome).toBe("data_starved");
  });

  it("reports the observations it ended without ruling on, and omits the count on a legacy run", async () => {
    const started = await loop();
    bareEvidence(started.ledger);
    resolve(started.ledger, contenders(started)[0]!.hypothesis_id, "proven");
    controllerFor(started.ledger, []).terminate("completed");

    // A hunt that stopped mid-classification says so rather than reading as if
    // every observation had been weighed.
    expect(finalized(started.ledger)[0]!.unruled).toBe(1);

    const legacy = await newLedger();
    bareEvidence(legacy.ledger);
    controllerFor(legacy.ledger, []).terminate("completed");
    // Absent, not zero: a legacy run was never asked to rule on anything, and a
    // new key would move the fold the #625 gate pins.
    expect(finalized(legacy.ledger)[0]!.unruled).toBeUndefined();
  });

  it("keeps the three endings distinct", async () => {
    const starved = await loop();
    await gapLock(starved.ledger, contenders(starved)[0]!.hypothesis_id);
    await controllerFor(starved.ledger, [ruled(starved.ledger, CONCLUDE)]).advanceIteration();

    const done = await loop();
    resolve(done.ledger, contenders(done)[0]!.hypothesis_id, "proven");
    await controllerFor(done.ledger, [ruled(done.ledger, CONCLUDE)]).advanceIteration();

    // "we could not look" and "we cleared it" must not read the same to an analyst.
    expect(finalized(starved.ledger)[0]!.outcome).toBe("data_starved");
    expect(finalized(done.ledger)[0]!.outcome).toBe("completed");
    expect(finalized(starved.ledger)[0]!.outcome).not.toBe(finalized(done.ledger)[0]!.outcome);
  });
});

// The refusal a live run died on. The lead put a hypothesis id in target_entity;
// the refusal was right but said only that the graph was empty, so all three
// attempts repeated it and the run failed on the attempt bound.
describe("refusing an entity the graph does not know", () => {
  it("says what target_entity holds and that an empty graph admits none", async () => {
    const started = await loop({ hypotheses: ["h one"] });
    const stray: Decision = { ...INVESTIGATE, target_entity: "hypothesis:h-cf7fbf91" };

    expect(() => validateDecision(stray, started.ledger.projection)).toThrow(/target_entity names a thing/);
    expect(() => validateDecision(stray, started.ledger.projection)).toThrow(/leave it unset/);
  });

  it("names what the graph does know once evidence has arrived", async () => {
    const started = await loop({ hypotheses: ["h one"] });
    evidenceOn(started.ledger, [...started.ledger.projection.hypotheses.keys()][0]!, {
      entities: [SEED_IP],
    });
    const stray: Decision = { ...INVESTIGATE, target_entity: "host:nowhere" };

    expect(() => validateDecision(stray, started.ledger.projection)).toThrow(/the graph knows /);
  });
});

// A failed dispatch is evidence about this deployment, and its text is ours. The
// extractor read IPs out of "read tcp 172.18.0.3:46528->160.79.104.10:443" -- a
// Docker bridge address and the model gateway -- and put them on the board as
// observables. A worker then spent a turn deciding whether api.anthropic.com was
// attacker infrastructure and wrote that a Frothly host had beaconed to it.
describe("a hunt does not investigate its own plumbing", () => {
  // Through the real dispatch path: a worker that fails is how this record is
  // written, and the extraction runs where it is appended.
  const failingDispatcher = (reason: string) => ({
    dispatch: async (request: { dispatch_id: string }) => ({
      dispatch_id: request.dispatch_id,
      evidence: [],
      failed: true,
      failure_reason: reason,
      cost_usd: 0,
    }),
  });

  it("takes no entities from a tool failure, however many addresses it names", async () => {
    const started = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    const controller = controllerFor(started.ledger, [INVESTIGATE], {
      dispatcher: failingDispatcher(
        "Error reading stream: read tcp 172.18.0.3:46528->160.79.104.10:443: read: connection timed out",
      ) as never,
    });

    await controller.advanceIteration();

    const record = [...started.ledger.projection.evidence.values()].find(
      (one) => one.provenance === "tool_failure",
    );
    // Two defences, and the summary one matters most: salienceFloor promotes a
    // tool_failure to anomalous, so whatever this says is the most prominent thing
    // the lead reads. The reason stays reachable for the operator, in the payload.
    expect(record?.summary).not.toMatch(/160\.79\.104\.10/);
    expect(record?.summary).not.toMatch(/172\.18\.0\.3/);
    expect(record?.payload["failure_reason"]).toMatch(/160\.79\.104\.10/);
    expect(record?.entities).toEqual([]);
  });

  // A dispatch that ran real queries and then died at the write-up. Discarding the
  // rows made one flaky call cost the whole iteration.
  const dyingAfterCalls = (reason: string) => ({
    dispatch: async (request: { dispatch_id: string }) => ({
      dispatch_id: request.dispatch_id,
      evidence: salvaged([
        {
          tool: "splunk_execute",
          args: '{"spl_query":"index=botsv3 | stats count by dest_ip"}',
          result: { ok: true as const, rows: [{ dest_ip: "45.77.53.176", count: 412 }], rowCount: 1, capped: false, sourceSystem: "cisco:asa" },
          wrapped: { text: "", scanned: false, verbs: [] },
        },
      ] as never),
      failed: true,
      failure_reason: reason,
      cost_usd: 0,
    }),
  });

  it("keeps the rows a dispatch gathered before its write-up died", async () => {
    const started = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    const controller = controllerFor(started.ledger, [INVESTIGATE], {
      dispatcher: dyingAfterCalls("read tcp 172.18.0.3:46528->160.79.104.10:443: read: connection timed out") as never,
    });

    await controller.advanceIteration();

    const records = [...started.ledger.projection.evidence.values()];
    // Both: the gap says the hunt could not finish looking, the salvage says what
    // it saw before it stopped. Either alone misreports the iteration.
    expect(records.filter((one) => one.provenance === "tool_failure")).toHaveLength(1);
    const kept = records.filter((one) => one.provenance === "unsummarised");
    expect(kept).toHaveLength(1);
    expect(kept[0]!.source_system).toBe("cisco:asa");
    // The estate's address, from the payload -- which is the whole point of keeping it.
    expect(kept[0]!.entities).toEqual([{ type: "ip", value: "45.77.53.176" }]);
    // Nothing vouched for these rows, so they cannot clear a branch on their own.
    expect(kept[0]!.attacker_influenceable).toBe(true);
  });

  // The estate's own addresses still have to reach the board: this refuses a
  // source, not a shape. Same path, same kind of text, opposite provenance.
  it("still takes entities from a worker's real answer", async () => {
    const started = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    const controller = controllerFor(started.ledger, [INVESTIGATE], {
      dispatcher: {
        dispatch: async (request: { dispatch_id: string }) => ({
          dispatch_id: request.dispatch_id,
          evidence: [
            {
              source_system: "net_flow",
              summary: "HOST-42 reached 45.77.53.176 every 30s",
              payload: {},
              salience: "notable" as const,
              why_notable: "low jitter",
              provenance: "worker",
              attacker_influenceable: false,
              instruction_like: false,
            },
          ],
          failed: false,
          cost_usd: 0,
        }),
      } as never,
    });

    await controller.advanceIteration();

    const record = [...started.ledger.projection.evidence.values()].find(
      (one) => one.provenance === "worker",
    );
    expect(record?.entities.map((one) => one.value)).toContain("45.77.53.176");
  });
});
