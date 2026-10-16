// A hunt links the alerts its evidence cites to its case -- and only those that bear on
// one of the case's own explanations (#2289).
import { describe, expect, it } from "vitest";
import { citedFindings, findingsCitedBy } from "../../plugins/hunt/findings.js";
import { newId } from "../../plugins/hunt/ids.js";
import type { Journal } from "../../plugins/hunt/journal.js";
import { BASE_RATE_PROVENANCE } from "../../plugins/hunt/opening.js";
import type { EvidenceRecord, LinkRelation } from "../../plugins/hunt/types.js";
import { newLedger } from "../support/hunt.js";

function record(ledger: Journal, payload: Record<string, unknown>): EvidenceRecord {
  const evidence: EvidenceRecord = {
    evidence_id: newId("ev"),
    dispatch_id: null,
    iteration: 1,
    source_system: "endpoint",
    summary: "rows",
    payload,
    salience: "notable",
    why_notable: "",
    provenance: "worker",
    attacker_influenceable: false,
    instruction_like: false,
    entities: [],
    captured_at: new Date().toISOString(),
  };
  ledger.append({ kind: "evidence", payload: evidence });
  return evidence;
}

function rule(ledger: Journal, evidence: EvidenceRecord, hypothesisId: string, relation: LinkRelation): void {
  ledger.append({ kind: "link", payload: { evidence_id: evidence.evidence_id, hypothesis_id: hypothesisId, relation } });
}

async function board() {
  const { ledger } = await newLedger({ hypothesisLoop: true });
  const hypotheses = [...ledger.projection.hypotheses.values()];
  const claim = hypotheses.find((one) => one.provenance !== BASE_RATE_PROVENANCE)!.hypothesis_id;
  const benign = hypotheses.find((one) => one.provenance === BASE_RATE_PROVENANCE)!.hypothesis_id;
  return { ledger, claim, benign };
}

describe("the alerts a record's rows cite", () => {
  it("names each finding_id its rows carry, once, however deep a salvaged record keeps them", async () => {
    const { ledger } = await board();
    const one = record(ledger, {
      rows: [{ finding_id: "demo2-ts-004", eventName: "CreateAccessKey" }, { finding_id: "demo2-ts-001" }, { finding_id: "demo2-ts-004" }],
      gathered: [{ tool: "findings_search", rows: [{ finding_id: "demo2-ts-007" }] }],
    });

    expect(findingsCitedBy(one).sort()).toEqual(["demo2-ts-001", "demo2-ts-004", "demo2-ts-007"]);
  });

  it("names nothing for rows that cite no finding", async () => {
    const { ledger } = await board();

    expect(findingsCitedBy(record(ledger, { rows: 3, src_ip: "45.77.53.176", finding_id: "", other: { finding_id: 7 } }))).toEqual([]);
  });
});

describe("the alerts a hunt links onto its case", () => {
  it("takes the one whose evidence supports the case's explanation and leaves the one that bears on neither", async () => {
    const { ledger, claim, benign } = await board();
    const chain = record(ledger, { rows: [{ finding_id: "dl-003", host: "FIN-WS-114" }] });
    const noise = record(ledger, { rows: [{ finding_id: "dl-008", host: "HR-WS-031" }] });
    rule(ledger, chain, claim, "supports");
    rule(ledger, noise, claim, "neither");
    rule(ledger, noise, benign, "neither");

    expect(citedFindings(ledger.projection)).toEqual(["dl-003"]);
  });

  it("takes one whose evidence weakens the case's explanation: it bears on the case", async () => {
    const { ledger, claim } = await board();
    rule(ledger, record(ledger, { finding_id: "dl-007" }), claim, "weakens");

    expect(citedFindings(ledger.projection)).toEqual(["dl-007"]);
  });

  it("leaves one whose evidence only supports the benign explanation on the run's record", async () => {
    const { ledger, claim, benign } = await board();
    const patching = record(ledger, { finding_id: "dl-009" });
    rule(ledger, patching, benign, "supports");
    rule(ledger, patching, claim, "neither");

    expect(citedFindings(ledger.projection)).toEqual([]);
  });

  it("follows the latest ruling: a link re-ruled to neither no longer bears on the case", async () => {
    const { ledger, claim } = await board();
    const lookup = record(ledger, { finding_id: "dl-012" });
    rule(ledger, lookup, claim, "supports");
    rule(ledger, lookup, claim, "neither");

    expect(citedFindings(ledger.projection)).toEqual([]);
  });

  it("leaves evidence nobody ruled on, and evidence citing no finding, out", async () => {
    const { ledger, claim } = await board();
    record(ledger, { finding_id: "dl-010" });
    rule(ledger, record(ledger, { rows: 3 }), claim, "supports");

    expect(citedFindings(ledger.projection)).toEqual([]);
  });
});
