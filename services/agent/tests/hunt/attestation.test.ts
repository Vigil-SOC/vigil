// Attacker-caused is not attacker-authored. In a hunt the adversary's own behaviour is
// the whole of the signal, so every record is attacker-caused; a verdict has to ask
// something narrower -- whether the values it rests on were attested by the telemetry
// or chosen by the adversary. One real run flagged all eight of its records
// attacker-influenceable and could not have proven anything at any budget.
import { describe, expect, it } from "vitest";
import { evidenceFrom } from "../../workflows/hunt/adapters.js";
import { InvalidDecision, validateDecision } from "../../workflows/hunt/controller.js";
import { DEFAULT_VERDICTS } from "../../workflows/hunt/config.js";
import { evidenceStrength, sensorAttested, unmetPredicates } from "../../workflows/hunt/strength.js";
import type { EvidenceRecord } from "../../workflows/hunt/types.js";
import { evidenceOn, newLedger } from "../support/hunt.js";

const SENSOR = { field: "conn_count", authored: "sensor" as const };
const AUTHORED = { field: "filename", authored: "adversary" as const };
const FEED = { field: "threat_label", authored: "third_party" as const };

const record = (over: Partial<EvidenceRecord> = {}): EvidenceRecord =>
  ({
    evidence_id: "ev-1",
    dispatch_id: null,
    iteration: 1,
    source_system: "net_flow",
    summary: "3,885 connections at a 30s interval",
    payload: {},
    salience: "anomalous",
    why_notable: "low-jitter beaconing",
    provenance: "worker",
    attacker_influenceable: true,
    instruction_like: false,
    entities: [],
    captured_at: new Date().toISOString(),
    ...over,
  }) as EvidenceRecord;

describe("what a finding rests on", () => {
  it("attests a record with a sensor-counted basis, whatever else it carries", () => {
    expect(sensorAttested(record({ rests_on: [SENSOR, AUTHORED] }))).toBe(true);
  });

  it("does not attest one resting only on values the adversary chose", () => {
    expect(sensorAttested(record({ rests_on: [AUTHORED] }))).toBe(false);
  });

  // Somebody else's claim about the world is nobody's observation of this estate.
  it("does not attest a third party's claim", () => {
    expect(sensorAttested(record({ rests_on: [FEED, AUTHORED] }))).toBe(false);
  });

  // A ledger written before the split has the boolean and nothing else, and has to read
  // exactly as it did.
  it("falls back to the record-level flag where no basis was named", () => {
    expect(sensorAttested(record({ attacker_influenceable: true }))).toBe(false);
    expect(sensorAttested(record({ attacker_influenceable: false }))).toBe(true);
  });
});

describe("what a verdict may rest on", () => {
  it("stops blocking a claim whose support the telemetry attested", async () => {
    const { ledger, hypothesisIds } = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    const hypothesisId = hypothesisIds[0]!;
    // The shape of the real run: the adversary picked the destination and the interval,
    // the sensor counted the connections.
    evidenceOn(ledger, hypothesisId, { source: "net_flow", attackerInfluenceable: true, restsOn: [SENSOR, AUTHORED] });
    evidenceOn(ledger, hypothesisId, { source: "endpoint", attackerInfluenceable: true, restsOn: [{ field: "parent_process", authored: "sensor" }] });

    const strength = evidenceStrength(ledger.projection, hypothesisId);

    expect(strength.attacker_influenceable_only).toBe(false);
    expect(strength.corroborating_sources).toBe(2);
    expect(unmetPredicates(strength, DEFAULT_VERDICTS).filter((why) => why.includes("attested"))).toEqual([]);
  });

  it("still blocks a claim resting only on what the adversary wrote", async () => {
    const { ledger, hypothesisIds } = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    const hypothesisId = hypothesisIds[0]!;
    evidenceOn(ledger, hypothesisId, { source: "http", attackerInfluenceable: true, restsOn: [AUTHORED] });
    evidenceOn(ledger, hypothesisId, { source: "intel", attackerInfluenceable: true, restsOn: [FEED] });

    const strength = evidenceStrength(ledger.projection, hypothesisId);

    expect(strength.attacker_influenceable_only).toBe(true);
    expect(unmetPredicates(strength, DEFAULT_VERDICTS).join(" ")).toMatch(/attested/);
  });
});

// The gate this flag was built for, and the one direction it was always right in: an
// adversary must not be able to talk the hunt out of a branch by writing a benign value.
describe("what an ABANDON may rest on", () => {
  it("refuses a drop citing only values the adversary chose", async () => {
    const { ledger, hypothesisIds } = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    const hypothesisId = hypothesisIds[0]!;
    const cited = evidenceOn(ledger, hypothesisId, { attackerInfluenceable: true, restsOn: [AUTHORED] });

    expect(() =>
      validateDecision(
        { action: "ABANDON", rationale: "the filename looks like an update", target_hypothesis_id: hypothesisId, evidence_citations: [cited] },
        ledger.projection,
      ),
    ).toThrow(InvalidDecision);
  });

  it("allows one citing something the telemetry attested", async () => {
    const { ledger, hypothesisIds } = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    const hypothesisId = hypothesisIds[0]!;
    const cited = evidenceOn(ledger, hypothesisId, { attackerInfluenceable: true, restsOn: [SENSOR] });

    expect(() =>
      validateDecision(
        { action: "ABANDON", rationale: "the interval is irregular in the counts", target_hypothesis_id: hypothesisId, evidence_citations: [cited] },
        ledger.projection,
      ),
    ).not.toThrow();
  });
});

describe("reading a worker's emission", () => {
  const emit = (rests: unknown) =>
    evidenceFrom({ results: [{ source_system: "net_flow", summary: "s", why_notable: "w", salience: "anomalous", payload: "{}", rests_on: rests }] })[0]!;

  it("carries a basis the worker named", () => {
    expect(emit([SENSOR, AUTHORED]).rests_on).toEqual([SENSOR, AUTHORED]);
  });

  // sensorAttested reads this to decide whether a claim can carry a verdict, so an
  // authorship this side does not know must never read as an attestation.
  it("drops an entry naming an authorship it does not know", () => {
    expect(emit([{ field: "x", authored: "vendor" }, SENSOR]).rests_on).toEqual([SENSOR]);
  });

  it("drops an entry naming no field", () => {
    expect(emit([{ field: "  ", authored: "sensor" }]).rests_on).toBeUndefined();
  });

  it("leaves the basis absent when the worker named none", () => {
    expect(emit(undefined).rests_on).toBeUndefined();
    expect(emit("sensor").rests_on).toBeUndefined();
  });
});
