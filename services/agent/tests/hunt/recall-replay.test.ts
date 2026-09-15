import { describe, expect, it } from "vitest";
import { gunzipSync } from "node:zlib";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { prefixBytes, prefixMessages, prefixOf } from "../../core/context.js";
import { recalledNotes, recalledRowsOf, type RecallResult } from "../../contracts/memory.js";
import { fold, type HuntEvent } from "../../workflows/hunt/ledger.js";
import { replay } from "../../workflows/hunt/replay.js";

// The replay gate for episodic recall (#737).
//
// Its fixture is a hunt recorded by current code, not a Golden: a Golden is the
// output of the implementation being replaced, the pre-harness file ledger
// produces nothing any more, and no run of the port can have that provenance
// (ADR 0012). So this is a regression snapshot, it lives apart from the ten
// tests/fixtures/runs the Fold Equivalence gate replays, and it is written by
// scripts/record-recall-run.ts.
//
// What it holds to: a decision was made against the rows the run was given, and a
// rebuild presents those rows rather than whatever memory holds now. Nothing here
// reaches a Memory -- that is the point, and it is structural: every function
// under test takes the log and nothing else.

const FIXTURE = join(import.meta.dirname, "..", "fixtures", "replay", "hunt-recall.jsonl.gz");

const events = (): HuntEvent[] =>
  gunzipSync(readFileSync(FIXTURE))
    .toString("utf8")
    .split("\n")
    .filter((line) => line.trim() !== "")
    .map((line) => JSON.parse(line) as HuntEvent);

const LOG = events();

// Everything below reads the rebuild through replay(), not through the renderer
// beside it: the check is that a Replay presents the recalled rows, so a replay
// that stopped rebuilding them has to fail here rather than pass next to a
// separate call that still does.
const recalledBy = (log: readonly HuntEvent[]): readonly string[] => replay(log).recalled;

// The lead's opening turn, rebuilt. Tools are left out because prefixMessages
// never reads them: recall reaches the opening user turn and nowhere else.
const openingFrom = (log: readonly HuntEvent[]): string | undefined =>
  prefixMessages(prefixOf("the lead's prompt", [], recalledBy(log)), "the task")[1]?.content;

// What the prompt cache is keyed on, which is what a rebuild has to reproduce.
const bytesFrom = (log: readonly HuntEvent[]): string =>
  prefixBytes(prefixOf("the lead's prompt", [], recalledBy(log)));

describe("the recorded run carries what it recalled", () => {
  it("journals one recall event for the whole run", () => {
    expect(LOG.filter((event) => event.kind === "recall")).toHaveLength(1);
  });

  it("carries a result the contract can read, key for key", () => {
    expect(recalledRowsOf(LOG)).not.toBeNull();
  });

  // The rows were journaled before anything decided against them, so no decision
  // in this run was made on a set the rebuild cannot reach.
  it("journals the read before the first decision", () => {
    const recall = LOG.findIndex((event) => event.kind === "recall");
    const first = LOG.findIndex((event) => event.kind === "decision");
    expect(recall).toBeGreaterThanOrEqual(0);
    expect(recall).toBeLessThan(first);
  });

  it("was a run that actually decided something against those rows", () => {
    expect(LOG.filter((event) => event.kind === "decision").length).toBeGreaterThan(0);
  });

  it("shows the recalled verdict in the bytes the lead opened on", () => {
    expect(openingFrom(LOG)).toContain("192.0.2.10 is a scheduled backup target");
  });

  it("names what the read left behind rather than presenting a truncated set as whole", () => {
    expect(openingFrom(LOG)).toContain("Some history was not carried");
  });
});

describe("a rebuild reads the journaled rows", () => {
  // One read for eight lead turns, so every turn of the run was shown the same
  // bytes: a read per turn is what would have moved them (ADR 0009).
  it("holds one read for every turn the run took", () => {
    expect(LOG.filter((event) => event.kind === "recall")).toHaveLength(1);
    expect(LOG.filter((event) => event.kind === "decision").length).toBeGreaterThan(1);
  });

  // Selection was pinned when the rows were journaled. A rebuild that re-capped or
  // re-sorted under today's parameters would present a decision with a set it was
  // never made against, and nothing in the rebuilt prefix would say so.
  it("rebuilds the same cache key when the ranking parameters have since moved", () => {
    const drifted = LOG.map((event) =>
      event.kind === "recall"
        ? { ...event, payload: { ...(event.payload as RecallResult), ranking: { order: "salience", per_key_cap: 1, overall_cap: 1 } } }
        : event,
    ) as HuntEvent[];
    expect(bytesFrom(drifted)).toBe(bytesFrom(LOG));
  });

  // What the contract's own comment names as the failure that hides: a payload
  // missing a key would render as an entity nobody has looked at.
  it("presents nothing for a payload it cannot read, rather than a prefix of holes", () => {
    const { keys: _gone, ...missing } = recalledRowsOf(LOG) as RecallResult;
    const drifted = LOG.map((event) => (event.kind === "recall" ? { ...event, payload: missing } : event)) as HuntEvent[];
    expect(recalledBy(drifted)).toEqual([]);
  });

  it("has nothing to present when the event is missing, rather than reading memory for it", () => {
    const without = LOG.filter((event) => event.kind !== "recall");
    expect(recalledBy(without)).toEqual([]);
  });
});

describe("the fold and the replay are unmoved by it", () => {
  // No fold reads episodic: the projection is one run's account of itself, and a
  // recalled row is an input to a decision rather than a belief the hunt holds.
  it("folds to the same projection with the recall event and without it", () => {
    expect(fold(LOG)).toEqual(fold(LOG.filter((event) => event.kind !== "recall")));
  });

  // replay.ts had no caller until this test. Every decision the run made rebuilds
  // to the digest it was journaled against, recall on the prefix included.
  it("reproduces every digest the run decided against", () => {
    const report = replay(LOG);
    expect(report.decisions.length).toBeGreaterThan(0);
    expect(report.decisions.filter((decision) => decision.mismatch !== null)).toEqual([]);
    expect(report.reproduced).toBe(report.decisions.length);
  });

  // What Postgres does to the recorded digest: jsonb keeps no key order, so a
  // ledger read back from the store presents the same digest in different bytes.
  it("reads a recorded digest with its keys reordered as the same digest", () => {
    const reordered = (value: unknown): unknown => {
      if (Array.isArray(value)) return value.map(reordered);
      if (typeof value !== "object" || value === null) return value;
      const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) => (a > b ? -1 : 1));
      return Object.fromEntries(entries.map(([key, held]) => [key, reordered(held)]));
    };
    const stored = LOG.map((event) =>
      event.kind === "decision" ? { ...event, payload: { ...event.payload, digest_presented: reordered(event.payload.digest_presented) } } : event,
    ) as HuntEvent[];
    expect(replay(stored).decisions.filter((decision) => decision.mismatch !== null)).toEqual([]);
  });

  it("rebuilds each digest exactly, with no inferred prefix", () => {
    expect(replay(LOG).inexact).toBe(0);
  });

  // The other half of what a decision was shown. A Replay that rebuilt only the
  // digest would reproduce every decision in this run and still be blind to the
  // rows the lead actually opened on.
  it("rebuilds the recalled rows alongside the digests", () => {
    const report = replay(LOG);
    expect(report.recalled.join("\n")).toContain("192.0.2.10 is a scheduled backup target");
    expect(report.recalled).toEqual(recalledNotes(recalledRowsOf(LOG) as RecallResult));
  });
});
