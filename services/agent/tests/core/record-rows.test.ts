import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { recordOf, recordRows } from "../../core/record.js";

// The shared fold on a kernel investigate ledger: the hunt's side is in tests/hunt/record-rows.test.ts.

const events = readFileSync(join(import.meta.dirname, "..", "fixtures", "ledgers", "kernel-investigate.jsonl"), "utf8")
  .split("\n")
  .filter((line) => line.trim() !== "")
  .map((line) => JSON.parse(line) as { kind: string; payload: unknown });
const linked = ["r2", "r3", "r9"];

describe("the record rows of a kernel investigate ledger", () => {
  it("counts each stance, and neither over the rows the caller counts", () => {
    const { hypotheses } = recordRows(recordOf(events), linked);
    expect(hypotheses.map(({ hypothesis_id, supports, weakens, neither }) => ({ hypothesis_id, supports, weakens, neither }))).toEqual([
      { hypothesis_id: "h1", supports: 1, weakens: 2, neither: 0 },
      { hypothesis_id: "h2", supports: 0, weakens: 0, neither: 3 },
      { hypothesis_id: "h3", supports: 2, weakens: 0, neither: 1 },
    ]);
    expect(recordRows(recordOf(events), []).hypotheses.every((row) => row.neither === 0)).toBe(true);
  });

  it("keeps the later stance on a re-weighed row, and applies patches", () => {
    const later = [
      ...events,
      { kind: "link", payload: { evidence_id: "r2", hypothesis_id: "h1", relation: "weakens" } },
      { kind: "patch", payload: { target: "hypothesis", id: "h1", fields: { status: "disproven", resolution_reason: "the tool was signed" } } },
      { kind: "question", payload: { question_id: "q1", question: "who signed it?", status: "open", hypothesis_id: "h1" } },
      { kind: "question", payload: { question_id: "q2", question: "settled", status: "closed", hypothesis_id: null } },
    ];
    const rows = recordRows(recordOf(later), linked);
    expect(rows.hypotheses[0]).toMatchObject({ status: "disproven", resolution_reason: "the tool was signed", supports: 0, weakens: 3 });
    expect(rows.bears_on.get("r2")).toEqual([{ hypothesis_id: "h1", relation: "weakens" }]);
    expect(rows.bears_on.get("r3")).toEqual([
      { hypothesis_id: "h1", relation: "weakens" },
      { hypothesis_id: "h3", relation: "supports" },
    ]);
    expect(rows.open_questions).toEqual([{ question_id: "q1", question: "who signed it?", hypothesis_id: "h1" }]);
  });
});
