// A hunt links the alerts its evidence cites to its case -- and only those.
import { describe, expect, it } from "vitest";
import { citedFindings } from "../../workflows/hunt/findings.js";
import { newId } from "../../workflows/hunt/ids.js";
import { newLedger } from "../support/hunt.js";

async function withPayloads(...payloads: Record<string, unknown>[]) {
  const { ledger } = await newLedger();
  for (const payload of payloads) {
    ledger.append({
      kind: "evidence",
      payload: {
        evidence_id: newId("ev"),
        dispatch_id: null,
        iteration: 1,
        source_system: "duckdb",
        summary: "rows",
        payload,
        salience: "notable",
        why_notable: "",
        provenance: "worker",
        attacker_influenceable: false,
        instruction_like: false,
        entities: [],
        captured_at: new Date().toISOString(),
      },
    });
  }
  return ledger.projection;
}

describe("the alerts a hunt's evidence cites", () => {
  it("names each finding_id its evidence rows carry, once", async () => {
    const projection = await withPayloads(
      { rows: [{ finding_id: "demo2-ts-004", eventName: "CreateAccessKey" }, { finding_id: "demo2-ts-001" }] },
      { finding_id: "demo2-ts-004" },
      // A salvaged record keeps the rows a dispatch gathered a level further down.
      { gathered: [{ tool: "findings_search", rows: [{ finding_id: "demo2-ts-007" }] }] },
    );

    expect(citedFindings(projection).sort()).toEqual(["demo2-ts-001", "demo2-ts-004", "demo2-ts-007"]);
  });

  it("names nothing for evidence that cites no finding", async () => {
    const projection = await withPayloads({ rows: 3, src_ip: "45.77.53.176" }, { finding_id: "" }, { finding_id: 7 });

    expect(citedFindings(projection)).toEqual([]);
  });
});
