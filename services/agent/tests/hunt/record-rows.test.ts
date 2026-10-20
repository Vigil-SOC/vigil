import { describe, expect, it } from "vitest";
import { recordRows } from "../../core/record.js";
import { fold } from "../../plugins/hunt/ledger.js";
import { huntProjection } from "../../plugins/hunt/projection.js";
import { NULL_CHECK_PROVENANCE } from "../../plugins/hunt/strength.js";
import { evidenceOn, newLedger } from "../support/hunt.js";

// The shared fold on a hunt ledger, counting neither over every row but the critic's verdicts.
describe("the record rows of a hunt", () => {
  it("are the rows its projection carries, a critic verdict counted by nothing", async () => {
    const started = await newLedger({ hypotheses: ["a host is beaconing to C2", "the traffic is a backup agent"] });
    const [beacon, backup] = started.hypothesisIds as [string, string];
    evidenceOn(started.ledger, beacon);
    const reweighed = evidenceOn(started.ledger, beacon);
    started.ledger.append({ kind: "link", payload: { evidence_id: reweighed, hypothesis_id: beacon, relation: "weakens" } });
    const verdict = evidenceOn(started.ledger, backup, { relation: "neither" });
    await started.ledger.flush();
    const events = await started.state.read(started.runId);
    const held = events.find((event) => event.kind === "evidence" && (event.payload as { evidence_id: string }).evidence_id === verdict);
    (held?.payload as { provenance: string }).provenance = NULL_CHECK_PROVENANCE;

    const view = fold(events);
    const counted = [...view.evidence.values()].filter((one) => one.provenance !== NULL_CHECK_PROVENANCE).map((one) => one.evidence_id);
    const rows = recordRows(view, counted);
    expect(rows.hypotheses.map(({ supports, weakens, neither }) => ({ supports, weakens, neither }))).toEqual([
      { supports: 1, weakens: 1, neither: 0 },
      { supports: 0, weakens: 0, neither: 2 },
    ]);
    expect(rows.bears_on.get(reweighed)).toEqual([{ hypothesis_id: beacon, relation: "weakens" }]);

    const projected = huntProjection(started.runId, events);
    expect(projected.hypotheses).toEqual(rows.hypotheses.map((row) => expect.objectContaining(row)));
    expect(projected).toMatchObject({ run_kind: "hunt", max_cost_usd: projected.budgets.max_cost_usd });
  });
});
