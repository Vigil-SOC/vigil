import { describe, expect, it } from "vitest";
import { InProcessState } from "../../core/state.js";
import { asHarnessEvents, gunzipped, historicalRuns, renamedGolden } from "../support/historical.js";
import { fold, LedgerError, projectionOf, type HuntKinds } from "../../workflows/hunt/ledger.js";
import { digestOf } from "../../workflows/hunt/config.js";
import { buildDigest } from "../../workflows/hunt/digest.js";
import { evidenceStrength } from "../../workflows/hunt/strength.js";
import { buildReport } from "../../workflows/hunt/report.js";

const RUNS = historicalRuns();

// Maps do not survive JSON, and the goldens were written from the file ledger's
// fold. Ordering is preserved: a Map keeps insertion order and so does this.
function comparable(projection: ReturnType<typeof fold>): unknown {
  return JSON.parse(
    JSON.stringify({
      ...projection,
      hypotheses: Object.fromEntries(projection.hypotheses),
      questions: Object.fromEntries(projection.questions),
      evidence: Object.fromEntries(projection.evidence),
      dispatches: Object.fromEntries(projection.dispatches),
      checkpoints: Object.fromEntries(projection.checkpoints),
    }),
  );
}

// The one deliberate divergence from the file ledger's fold. It appended every
// link, so a record the lead re-ruled on each iteration ended up carrying the same
// hypothesis many times over -- and among those, "supports" beside "weakens". The
// harness fold upserts on the pair, so the golden's links are collapsed the same
// way here rather than the goldens being rewritten: everything else still compares
// against what the old implementation actually produced.
function lastPerPair(links: { evidence_id: string; hypothesis_id: string }[]): unknown[] {
  const held = new Map<string, unknown>();
  for (const link of links) held.set(`${link.evidence_id} ${link.hypothesis_id}`, link);
  return [...held.values()];
}

// Written by running the file ledger's own fold over the same fixture, with the
// same Map conversion applied, so this compares implementations and not shapes.
function golden(name: string): unknown {
  const held = renamedGolden(JSON.parse(gunzipped(`${name}.projection.json.gz`))) as Record<string, unknown>;
  return { ...held, links: lastPerPair(held["links"] as { evidence_id: string; hypothesis_id: string }[]) };
}

describe("the fold survives the move to the harness ledger", () => {
  it("has ten historical ledgers to replay", () => {
    expect(RUNS).toHaveLength(10);
  });

  it.each(RUNS)("%s folds to the projection the file ledger produced", (name) => {
    const events = asHarnessEvents(gunzipped(`${name}.jsonl.gz`), name);
    expect(comparable(fold(events))).toEqual(golden(name));
  });

  it.each(RUNS)("%s folds identically when read back through the State seam", async (name) => {
    const events = asHarnessEvents(gunzipped(`${name}.jsonl.gz`), name);
    const state = new InProcessState<HuntKinds>();
    await state.append(name, events.map(({ seq, ts, schema_version, ...rest }) => rest));

    expect(comparable(await projectionOf(state, name))).toEqual(comparable(fold(events)));
  });
});

describe("a ledger that is not one", () => {
  it("refuses a torn write rather than folding what it could parse", () => {
    const torn = gunzipped("torn.jsonl.corrupt.gz");
    expect(() => asHarnessEvents(torn, "torn")).toThrow(SyntaxError);
  });

  it("refuses a ledger that does not open with a run event", () => {
    const events = asHarnessEvents(gunzipped(`${RUNS[0]}.jsonl.gz`), RUNS[0]!).slice(1);
    expect(() => fold(events)).toThrow(LedgerError);
  });

  it("refuses a patch against a record the ledger never opened", () => {
    const events = asHarnessEvents(gunzipped(`${RUNS[0]}.jsonl.gz`), RUNS[0]!);
    events.push({
      ...events[0]!,
      seq: 999,
      kind: "patch",
      payload: { target: "hypothesis", id: "h-never", fields: { status: "proven" } },
    } as (typeof events)[number]);

    expect(() => fold(events)).toThrow(/unknown hypothesis h-never/);
  });
});

// Every fold, not only the projection: what a hunt produces is derived, so a
// port that folds to the same projection can still report something different.
function folds(name: string): unknown {
  const view = fold(asHarnessEvents(gunzipped(`${name}.jsonl.gz`), name));
  return JSON.parse(
    JSON.stringify({
      digest: buildDigest(view, view.hunt.iteration, digestOf(view.hunt.spec)),
      strength: Object.fromEntries([...view.hypotheses.keys()].map((id) => [id, evidenceStrength(view, id)])),
      report: buildReport(view),
    }),
  );
}

// The second deliberate divergence, and the same kind as lastPerPair: the goldens are
// what the old implementation produced, and its backlog listed only parked leads. A run
// stopped by its own ceiling hands its leads back open, so hunt-462b6e9d6d56 -- a real
// budget_terminated run -- reported an empty frontier while 82 leads sat on it.
//
// Set aside from the structural comparison and then checked against the golden's own
// backlog plus exactly the leads left open, rather than simply dropped: more than one
// run moved, and dropping the field would have left the other nine backlogs compared
// against nothing at all.
function withoutBacklog(folded: Record<string, unknown>): Record<string, unknown> {
  const report = folded["report"] as Record<string, unknown>;
  const { backlog: _dropped, ...rest } = report;
  return { ...folded, report: rest };
}

function backlogIds(folded: Record<string, unknown>): string[] {
  const report = folded["report"] as Record<string, unknown>;
  const backlog = (report["backlog"] ?? []) as { question_id: string }[];
  return backlog.map((one) => one.question_id).sort();
}

describe("the derived folds survive the move too", () => {
  it.each(RUNS)("%s derives the digest, strength and report the file ledger did", (name) => {
    const mine = folds(name) as Record<string, unknown>;
    const golden = renamedGolden(JSON.parse(gunzipped(`${name}.folds.json.gz`))) as Record<string, unknown>;
    expect(withoutBacklog(mine)).toEqual(withoutBacklog(golden));

    // Every lead the golden listed, and every lead still open, and nothing else: the
    // backlog stays exactly compared on all ten runs instead of excused on all ten.
    const view = fold(asHarnessEvents(gunzipped(`${name}.jsonl.gz`), name));
    const open = [...view.questions.values()].filter((one) => one.status === "open");
    expect(backlogIds(mine)).toEqual([...backlogIds(golden), ...open.map((one) => one.question_id)].sort());
  });

  // Named because it is the headline case: a real budget-stopped run whose report told
  // an operator the frontier was clear while 82 leads sat on it.
  it("lists the leads a budget-stopped run left on the frontier", () => {
    const view = fold(asHarnessEvents(gunzipped("hunt-462b6e9d6d56.jsonl.gz"), "hunt-462b6e9d6d56"));
    const open = [...view.questions.values()].filter((one) => one.status === "open");
    const report = buildReport(view);

    const parked = [...view.questions.values()].filter((one) => one.status === "parked");
    expect(view.hunt.outcome).toBe("budget_terminated");
    expect(open).toHaveLength(82);
    expect(report.backlog.map((one) => one.question_id)).toEqual(
      expect.arrayContaining([...open, ...parked].map((one) => one.question_id)),
    );
  });
});
