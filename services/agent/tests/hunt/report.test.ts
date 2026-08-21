// How a report reads, as opposed to what it contains. buildReport's gaps array is
// frozen by the ADR 0012 goldens, so everything here is presentation over the
// same records.
import { describe, expect, it } from "vitest";
import { citedTechniques } from "../../workflows/hunt/strength.js";
import { buildReport, groupedGaps, renderReport, type HuntReport, type VisibilityGap } from "../../workflows/hunt/report.js";
import { DEFAULT_BUDGETS } from "../../workflows/hunt/types.js";
import { evidenceOn, newLedger } from "../support/hunt.js";

// A fan-out hands every worker the same query_intent, so four failed workers
// printed one 300-character intent four times over and buried the reasons that
// actually differed.
const INTENT = "Determine reputation, ownership and ASN for 45.77.53.176";

const gap = (over: Partial<VisibilityGap> = {}): VisibilityGap => ({
  evidence_id: "ev-1",
  iteration: 2,
  summary: "worker failed: calls_exhausted",
  query_intent: INTENT,
  hypothesis_id: "h-cacab566",
  ...over,
});

const fannedOut: VisibilityGap[] = [
  gap({ evidence_id: "ev-1" }),
  gap({ evidence_id: "ev-2" }),
  gap({ evidence_id: "ev-3", summary: "worker failed: timeout" }),
  gap({ evidence_id: "ev-4" }),
];

describe("one row per question, not per worker", () => {
  it("collapses the workers that were asked the same thing", () => {
    const asked = groupedGaps(fannedOut);

    expect(asked).toHaveLength(1);
    expect(asked[0]!.workers).toBe(4);
  });

  it("keeps every distinct reason and drops the repeats", () => {
    expect(groupedGaps(fannedOut)[0]!.reasons).toEqual([
      "worker failed: calls_exhausted",
      "worker failed: timeout",
    ]);
  });

  it("keeps questions apart when they differ", () => {
    const other = gap({ evidence_id: "ev-9", query_intent: "something else" });
    expect(groupedGaps([...fannedOut, other])).toHaveLength(2);
  });

  it("keeps the same question apart across iterations", () => {
    const later = gap({ evidence_id: "ev-9", iteration: 3 });
    expect(groupedGaps([...fannedOut, later])).toHaveLength(2);
  });

  it("does not merge a gap that bears on no hypothesis into one that does", () => {
    const unattributed = gap({ evidence_id: "ev-9", hypothesis_id: null, query_intent: "" });
    expect(groupedGaps([unattributed, gap()])).toHaveLength(2);
  });
});

describe("the rendered visibility gaps", () => {
  const report = (gaps: VisibilityGap[]): HuntReport => ({
    hunt_id: "hunt-1",
    name: "threat-hunt",
    outcome: "budget_terminated",
    reason: "the budget refused another iteration",
    iterations: 2,
    cost_usd: 1.31,
    budgets: DEFAULT_BUDGETS,
    created_at: "2026-08-18T00:00:00.000Z",
    terminated_at: "2026-08-18T00:10:00.000Z",
    hypotheses: [],
    gaps,
    parked_hypotheses: [],
    backlog: [],
    checkpoints: [],
    suppressions: [],
    handoffs: [],
  });

  it("states the intent once however many workers failed on it", () => {
    const rendered = renderReport(report(fannedOut));
    expect(rendered.split(INTENT)).toHaveLength(2);
  });

  it("says how many workers were asked, so the count is not lost", () => {
    expect(renderReport(report(fannedOut))).toMatch(/\(4 workers\)/);
  });

  it("lists the reasons underneath rather than beside the intent", () => {
    const rendered = renderReport(report(fannedOut));
    expect(rendered).toMatch(/ {2}- worker failed: calls_exhausted/);
    expect(rendered).toMatch(/ {2}- worker failed: timeout/);
  });

  it("still says plainly when every query came back", () => {
    expect(renderReport(report([]))).toMatch(/None: every query the hunt wanted to run came back/);
  });
});

// The distinction the whole feature rests on: a hypothesis's declared technique
// is the playbook author's claim about what it tests; what evidence actually
// cited is what a worker observed. HuntReport itself never carries either --
// it is frozen by the ADR 0012 goldens -- so both are read off the live
// Projection at render time, the same way groupedGaps derives a rendering
// without changing what buildReport recorded.
describe("citing a technique beside a claim", () => {
  it("names distinct techniques evidence cited, not what the hypothesis declared", async () => {
    const started = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    const hypothesisId = started.hypothesisIds[0]!;
    const first = evidenceOn(started.ledger, hypothesisId, { attackTechnique: "T1071.001" });
    evidenceOn(started.ledger, hypothesisId, { attackTechnique: "T1071.001" });
    evidenceOn(started.ledger, hypothesisId, { attackTechnique: "T1496" });
    evidenceOn(started.ledger, hypothesisId); // no technique cited

    expect(citedTechniques(started.ledger.projection, hypothesisId)).toEqual(["T1071.001", "T1496"]);
    expect(first).toBeTruthy();
  });

  it("counts a technique cited on a weakening record too", async () => {
    const started = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    const hypothesisId = started.hypothesisIds[0]!;
    evidenceOn(started.ledger, hypothesisId, { attackTechnique: "T1071.004", relation: "weakens" });

    expect(citedTechniques(started.ledger.projection, hypothesisId)).toEqual(["T1071.004"]);
  });

  // The lead rules every observation against every active belief, so most rulings are
  // `neither` -- and a `neither` is the ruling that this record does not bear on this
  // belief. Counted, every hypothesis inherited every technique in the run and the
  // column read identically on all nine rows of a real hunt.
  it("ignores a technique on a record ruled not to bear on the hypothesis", async () => {
    const started = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    const hypothesisId = started.hypothesisIds[0]!;
    evidenceOn(started.ledger, hypothesisId, { attackTechnique: "T1071.001" });
    evidenceOn(started.ledger, hypothesisId, { attackTechnique: "T1496", relation: "neither" });

    expect(citedTechniques(started.ledger.projection, hypothesisId)).toEqual(["T1071.001"]);
  });

  it("names nothing for a hypothesis every record was ruled against", async () => {
    const started = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    const hypothesisId = started.hypothesisIds[0]!;
    evidenceOn(started.ledger, hypothesisId, { attackTechnique: "T1071.001", relation: "neither" });

    expect(citedTechniques(started.ledger.projection, hypothesisId)).toEqual([]);
  });

  it("names nothing for a hypothesis no cited record bears on", async () => {
    const started = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    expect(citedTechniques(started.ledger.projection, started.hypothesisIds[0]!)).toEqual([]);
  });

  // The split: attack_techniques is the vocabulary a citation is gated against and
  // never a label on a belief. It used to be paired against `hypotheses` by list
  // position, which asserted a technique nobody had checked and made hypothesis
  // order load-bearing. What a belief is about is what its evidence cited.
  it("reports the technique evidence cited, not one the vocabulary happened to list first", async () => {
    const started = await newLedger({
      hypotheses: ["a host is beaconing to C2"],
      attackTechniques: ["T1071.001", "T1496"],
    });
    const hypothesisId = started.hypothesisIds[0]!;
    expect(started.ledger.projection.hypotheses.get(hypothesisId)?.attack_technique).toBeNull();

    evidenceOn(started.ledger, hypothesisId, { attackTechnique: "T1496" });
    const rendered = renderReport(buildReport(started.ledger.projection), started.ledger.projection);

    expect(rendered).toMatch(/\*\*Techniques cited by evidence:\*\* T1496/);
    expect(rendered).not.toMatch(/Declared technique/);
  });

  it("renders neither line without a projection, and buildReport's own shape is untouched", async () => {
    const started = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    const built = buildReport(started.ledger.projection);

    expect(renderReport(built)).not.toMatch(/Declared technique|Techniques cited/);
    expect(Object.keys(built.hypotheses[0]!)).toEqual([
      "hypothesis_id",
      "statement",
      "status",
      "resolution_reason",
      "evidence_strength",
    ]);
  });

  it("says nothing when the hypothesis declared no technique and evidence cited none", async () => {
    const started = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    const rendered = renderReport(buildReport(started.ledger.projection), started.ledger.projection);

    expect(rendered).not.toMatch(/Declared technique|Techniques cited/);
  });
});

// A hunt that gathered thirty records and cleared nothing read exactly like a hunt
// that did nothing: the verdicts said how each belief stood, the gaps said what could
// not be looked at, and nothing said what came back.
describe("what the hunt found", () => {
  it("prints the records gathered, and what each one bears on", async () => {
    const started = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    const hypothesisId = started.hypothesisIds[0]!;
    evidenceOn(started.ledger, hypothesisId, { relation: "weakens" });
    const rendered = renderReport(buildReport(started.ledger.projection), started.ledger.projection);

    expect(rendered).toMatch(/## What the hunt found \(1\)/);
    expect(rendered).toMatch(/duckdb saw the identity authenticate/);
    expect(rendered).toMatch(new RegExp(`weakens ${hypothesisId}`));
  });

  it("says so when nothing came back, rather than leaving the section out", async () => {
    const started = await newLedger({ hypotheses: ["a host is beaconing to C2"] });
    const rendered = renderReport(buildReport(started.ledger.projection), started.ledger.projection);

    expect(rendered).toMatch(/## What the hunt found \(0\)/);
    expect(rendered).toMatch(/Nothing came back that was not a blind spot/);
  });
});

// "Reached the end of its frontier" is a claim about coverage, and a hunt an operator
// concluded early has leads nobody took.
describe("the one line an operator reads first", () => {
  const base = {
    hunt_id: "hunt-1",
    name: "threat-hunt",
    outcome: "completed" as const,
    reason: "an operator asked the hunt to conclude on what it had",
    iterations: 6,
    cost_usd: 0.85,
    budgets: DEFAULT_BUDGETS,
    created_at: "2026-08-20T00:00:00Z",
    terminated_at: "2026-08-20T00:30:00Z",
    hypotheses: [],
    gaps: [],
    parked_hypotheses: [],
    backlog: [],
    checkpoints: [],
    suppressions: [],
    handoffs: [],
  };

  it("does not claim the frontier was exhausted when leads were left open", () => {
    const rendered = renderReport({
      ...base,
      backlog: [{ question_id: "q-1", question: "45.77.53.176", reason: "below the priority floor" }],
    });

    expect(rendered).toMatch(/1 lead\(s\) were left open/);
    expect(rendered).not.toMatch(/reached the end of its frontier/);
  });

  it("says the frontier was exhausted when it was", () => {
    expect(renderReport(base)).toMatch(/reached the end of its frontier/);
  });
});
