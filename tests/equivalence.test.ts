import { createHash } from "node:crypto";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { fold, parseLog, type LedgerEvent } from "../ai/ledger.js";
import { replay } from "../ai/replay.js";

const RUNS = join(import.meta.dirname, "..", "runs");

// The digest carve-out: the snapshot leaves the fold's read path, so equivalence
// is asserted over the projection without it, and replay proves it survived.
const OMITTED = new Set(["digest_presented", "presented_evidence_ids"]);

function canonical(value: unknown): unknown {
  if (value instanceof Map) {
    return [...value.entries()].sort((a, b) => (a[0] < b[0] ? -1 : 1)).map(([key, item]) => [key, canonical(item)]);
  }
  if (Array.isArray(value)) return value.map(canonical);
  if (value !== null && typeof value === "object") {
    const source = value as Record<string, unknown>;
    const keys = Object.keys(source).filter((key) => !OMITTED.has(key)).sort();
    return Object.fromEntries(keys.map((key) => [key, canonical(source[key])]));
  }
  return value;
}

export function runFiles(): string[] {
  return readdirSync(RUNS)
    .filter((name) => name.endsWith(".jsonl") && !name.endsWith(".inbox.jsonl"))
    .sort();
}

// Through the ledger's own reader, so the legacy hoist is under test rather than
// bypassed: these files are all in the pre-split shape.
export function readEvents(name: string): LedgerEvent[] {
  return parseLog(readFileSync(join(RUNS, name), "utf8"));
}

// Hashed rather than inlined: the projections run to megabytes, and a hash that
// changes is the same signal as a diff that changes, for a fixture a human reads.
function measure(events: readonly LedgerEvent[]) {
  const report = replay(events);
  return {
    events: events.length,
    projection: createHash("sha256").update(JSON.stringify(canonical(fold(events)))).digest("hex"),
    reproduced: report.reproduced,
    inexact: report.inexact,
    decisions: report.decisions.map((decision) => ({
      decision_id: decision.decision_id,
      iteration: decision.iteration,
      action: decision.action,
      target: decision.target,
      exact: decision.exact,
      mismatch: decision.mismatch,
    })),
  };
}

describe("fold equivalence over historical runs", () => {
  const files = runFiles();

  it("covers every historical run file", () => {
    expect(files).toMatchSnapshot();
  });

  for (const name of files) {
    it(`${name} folds and replays as recorded`, () => {
      expect(measure(readEvents(name))).toMatchSnapshot();
    });
  }

  // Named rather than left to the snapshot: this is the run that reproduces in
  // full, so it is the one whose regression means the digest itself has drifted.
  it("reproduces every digest in the known-good baseline run", () => {
    const report = replay(readEvents("hunt-b8ba4410ae0f.jsonl"));
    expect(report.decisions).toHaveLength(5);
    expect(report.reproduced).toBe(5);
    expect(report.inexact).toBe(0);
  });
});
