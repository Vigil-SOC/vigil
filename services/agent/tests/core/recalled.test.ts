import { describe, expect, it } from "vitest";
import type {
  RecallResult,
  RecalledGap,
  RecalledSighting,
  RecalledVerdict,
} from "../../contracts/memory.js";
import type { AgentEvent } from "../../contracts/events.js";
import { emptyRecall, hasRecall, recalledNotesOf, recalledNotes } from "../../contracts/memory.js";

const SIGHTING: RecalledSighting = {
  entity_key: "ip:192.0.2.10",
  source_system: "zeek",
  hit_count: 4,
  attacker_influenceable: false,
  window: { first_seen: "2026-03-01", last_seen: "2026-03-04" },
  investigation_kind: "hunt",
  investigation_id: "hunt-0001",
  concluded_at: "2026-03-05",
};

const VERDICT: RecalledVerdict = {
  hypothesis_id: "hyp-1",
  statement: "192.0.2.10 is a scheduled backup target",
  outcome: "false_positive",
  rationale: "confirmed with the platform team",
  subject_entities: ["ip:192.0.2.10"],
  attacker_influenceable_only: false,
  trust: "analyst",
  window: { first_seen: "2026-02-01", last_seen: "2026-03-01" },
  window_source: "observed",
  sources: [{ source_system: "zeek", stance: "weakens", source_tier: "telemetry" }],
  investigation_kind: "case",
  investigation_id: "case-0007",
  concluded_at: "2026-03-02",
};

const GAP: RecalledGap = {
  hypothesis_id: "hyp-2",
  statement: "the host's process telemetry would settle this",
  disposition: "no_evidence_gathered",
  reason: "the EDR tool answered nothing on either attempt",
  subject_entities: ["host:host0001-L"],
  investigation_kind: "hunt",
  investigation_id: "hunt-0002",
  concluded_at: "2026-03-06",
};

const resultOf = (over: Partial<RecallResult> = {}): RecallResult => ({
  ...emptyRecall(["ip:192.0.2.10"], "2026-03-07T00:00:00.000Z"),
  sightings: [SIGHTING],
  verdicts: [VERDICT],
  gaps: [GAP],
  ...over,
});

const eventOf = (payload: RecallResult): AgentEvent<Record<never, never>> => ({
  run_id: "run-1",
  run_kind: "hunt",
  seq: 1,
  ts: "2026-03-07T00:00:00.000Z",
  kind: "recall",
  payload,
  schema_version: 1,
});

describe("recalled rows render to notes", () => {
  it("says nothing at all when nothing was recalled", () => {
    expect(recalledNotes(emptyRecall([], "2026-03-07T00:00:00.000Z"))).toEqual([]);
  });

  it("carries every journaled row", () => {
    expect(recalledNotes(resultOf())).toHaveLength(3);
  });

  it("puts what was concluded before where something was seen", () => {
    const notes = recalledNotes(resultOf());
    expect(notes[0]).toContain("192.0.2.10 is a scheduled backup target");
    expect(notes[1]).toContain("the host's process telemetry would settle this");
    expect(notes[2]).toContain("ip:192.0.2.10");
  });

  it("keeps each kind in the order it was journaled in", () => {
    const second: RecalledSighting = { ...SIGHTING, entity_key: "ip:192.0.2.11", source_system: "osquery" };
    const notes = recalledNotes(resultOf({ sightings: [second, SIGHTING] }));
    expect(notes[2]).toContain("ip:192.0.2.11");
    expect(notes[3]).toContain("ip:192.0.2.10");
  });

  // Which cap fired, not just how many rows went: one entity having more history
  // than its share and the read simply being broad are different facts, and a
  // reader acts on them differently.
  it("names what the read left behind, and which limit took it", () => {
    const dropped = {
      sightings: { per_key_cap: 12, overall_cap: 0 },
      verdicts: { per_key_cap: 0, overall_cap: 3 },
      gaps: { per_key_cap: 0, overall_cap: 0 },
    };
    const notes = recalledNotes(resultOf({ dropped }));
    expect(notes.at(-1)).toBe(
      "Some history was not carried: 12 sightings past one entity's share, 3 verdicts past the read's own limit.",
    );
  });

  it("marks an attacker-influenceable sighting, which no other line does", () => {
    const notes = recalledNotes(resultOf({ sightings: [{ ...SIGHTING, attacker_influenceable: true }] }));
    expect(notes[2]).toContain("attacker-influenceable");
  });

  // ADR 0015: the rationale is model prose, carried for a human and never for a
  // ranking. It is rendered, so a reader sees why; that is the whole of its reach.
  it("carries a verdict's rationale", () => {
    expect(recalledNotes(resultOf())[0]).toContain("confirmed with the platform team");
  });
});

describe("a rebuild reads the journaled rows and nothing else", () => {
  it("rebuilds the notes the run carried from its recall event", () => {
    const result = resultOf();
    expect(recalledNotesOf([eventOf(result)])).toEqual(recalledNotes(result));
  });

  it("has nothing to rebuild when the run journaled no recall", () => {
    expect(recalledNotesOf([])).toEqual([]);
  });

  // The gate the whole fixture exists for. Selection was pinned when the rows were
  // journaled; a rebuild that reranks under today's parameters presents a different
  // set to a decision than the one the decision was made against.
  it("renders the same notes however the ranking parameters read", () => {
    const journaled = resultOf({ ranking: { order: "recency", per_key_cap: 1, overall_cap: 1 } });
    const drifted = resultOf({ ranking: { order: "salience", per_key_cap: 50, overall_cap: 500 } });
    expect(recalledNotesOf([eventOf(drifted)])).toEqual(recalledNotesOf([eventOf(journaled)]));
  });

  // A read that could not be served is journaled as its own shape. Rendering it as
  // an empty result would say these entities have no history -- true of every
  // entity while memory is down, so nothing would look wrong.
  it("presents nothing for a read that could not be served", () => {
    const unavailable = { keys: ["ip:192.0.2.10"], as_of: "2026-03-07T00:00:00.000Z", unavailable: "the endpoint answered 503" };
    expect(recalledNotesOf([eventOf(unavailable as never)])).toEqual([]);
  });

  // Presence is what a run reads to decide whether to ask memory at all, and an
  // unavailable read is an answer: the run carries no rows rather than asking again.
  it("counts an unavailable read as a read that happened", () => {
    const unavailable = { keys: [], as_of: "2026-03-07T00:00:00.000Z", unavailable: "no token" };
    expect(hasRecall([eventOf(unavailable as never)])).toBe(true);
    expect(hasRecall([])).toBe(false);
  });

  it("counts a payload it cannot read at all as a read that happened", () => {
    const { verdicts: _gone, ...drifted } = resultOf();
    expect(hasRecall([eventOf(drifted as never)])).toBe(true);
    expect(recalledNotesOf([eventOf(drifted as never)])).toEqual([]);
  });

  it("reads the run's own recall and not a later one", () => {
    const first = resultOf();
    const later = resultOf({ verdicts: [{ ...VERDICT, statement: "recalled after the fact" }] });
    expect(recalledNotesOf([eventOf(first), eventOf(later)])).toEqual(recalledNotes(first));
  });
});
