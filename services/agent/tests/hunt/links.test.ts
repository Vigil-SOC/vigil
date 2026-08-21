import { describe, expect, it } from "vitest";
import { fold, type HuntEvent } from "../../workflows/hunt/ledger.js";
import { evidenceStrength } from "../../workflows/hunt/strength.js";
import { DEFAULT_BUDGETS, type EvidenceRecord, type HuntState, type Hypothesis, type LinkRelation } from "../../workflows/hunt/types.js";

// The Hunt Lead is shown the same observation every iteration and rules on it every
// iteration. Appending those rulings made one record carry a link per turn -- and
// among them "supports" beside "weakens", which evidenceStrength counted as both a
// corroborating source and a contradiction of the same belief.
const HUNT: HuntState = {
  hunt_id: "hunt-1",
  status: "active",
  iteration: 0,
  cost_usd: 0,
  outcome: null,
  budgets: DEFAULT_BUDGETS,
  spec: {} as HuntState["spec"],
} as HuntState;

const HYPOTHESIS: Hypothesis = { hypothesis_id: "h-1", statement: "a host beaconed out", status: "active" } as Hypothesis;

function record(id: string, source: string): EvidenceRecord {
  return {
    evidence_id: id,
    iteration: 1,
    summary: "something came back",
    source_system: source,
    salience: "anomalous",
    provenance: "worker",
    attacker_influenceable: false,
    payload: {},
    entities: [],
  } as unknown as EvidenceRecord;
}

function ledger(links: [string, LinkRelation][]): HuntEvent[] {
  const events: HuntEvent[] = [
    { seq: 0, kind: "run", payload: { hunt: HUNT } } as unknown as HuntEvent,
    { seq: 1, kind: "hypothesis", payload: HYPOTHESIS } as unknown as HuntEvent,
    { seq: 2, kind: "evidence", payload: record("ev-1", "dns") } as unknown as HuntEvent,
    { seq: 3, kind: "evidence", payload: record("ev-2", "net_flow") } as unknown as HuntEvent,
  ];
  return [
    ...events,
    ...links.map(([evidence_id, relation], at) => ({
      seq: 4 + at,
      kind: "link",
      payload: { evidence_id, hypothesis_id: "h-1", relation },
    }) as unknown as HuntEvent),
  ];
}

describe("a ruling replaces the last one on the same pair", () => {
  it("holds one link per evidence-hypothesis pair however often the lead re-rules", () => {
    const projection = fold(ledger([["ev-1", "supports"], ["ev-1", "supports"], ["ev-1", "supports"]]));
    expect(projection.links).toHaveLength(1);
  });

  it("keeps the latest relation rather than both", () => {
    const projection = fold(ledger([["ev-1", "supports"], ["ev-1", "weakens"]]));
    expect(projection.links).toEqual([{ evidence_id: "ev-1", hypothesis_id: "h-1", relation: "weakens" }]);
  });

  it("counts a contradiction once, not once per turn the lead restated it", () => {
    const projection = fold(ledger([["ev-1", "weakens"], ["ev-1", "weakens"], ["ev-2", "weakens"]]));
    expect(evidenceStrength(projection, "h-1").contradicting_records).toBe(2);
  });

  it("does not let a re-ruled record corroborate and contradict the same belief", () => {
    const projection = fold(ledger([["ev-1", "supports"], ["ev-2", "supports"], ["ev-1", "weakens"]]));
    const strength = evidenceStrength(projection, "h-1");
    expect(strength.corroborating_sources).toBe(1);
    expect(strength.contradicting_records).toBe(1);
  });

  it("leaves distinct pairs alone", () => {
    const projection = fold(ledger([["ev-1", "supports"], ["ev-2", "weakens"]]));
    expect(projection.links).toHaveLength(2);
  });
});
