import { describe, expect, it } from "vitest";
import { unboundCapabilities, type ToolSpec } from "../../core/spec.js";
import { pluginFor } from "../../plugins/registry.js";
import { DEFAULT_VERDICTS } from "../../plugins/hunt/config.js";
import { DEPLOYMENT_SOURCE } from "../../plugins/hunt/opening.js";
import { huntProjection } from "../../plugins/hunt/projection.js";
import { buildReport } from "../../plugins/hunt/report.js";
import { DEPLOYMENT_GAP_PROVENANCE, evidenceStrength, isGap, unmetPredicates } from "../../plugins/hunt/strength.js";
import { newLedger, provable, reopen, validate, type Started } from "../support/hunt.js";

const SEARCH: ToolSpec = { id: "elastic_elastic_search_logs", kind: "remote", provides: "telemetry_search" };

const gaps = (started: Started) => [...started.ledger.projection.evidence.values()].filter(isGap);
const summaries = (started: Started) => gaps(started).map((record) => record.summary);

describe("a capability nothing provides is a blind spot the hunt declares", () => {
  it("seeds one gap per capability no tool in this deployment answers", async () => {
    const started = await newLedger({ needs: ["telemetry_search", "indicator_lookup"] });

    // In words, never the capability's id (#2287): the row and the blind spot are read by people.
    expect(summaries(started)).toEqual([
      "No SIEM is connected, so the hunt could not search raw telemetry",
      "No threat-intel source is connected, so the hunt could not look up indicators",
    ]);
    for (const summary of summaries(started)) expect(summary).not.toMatch(/telemetry_search|indicator_lookup/);
    for (const record of gaps(started)) {
      expect(record.provenance).toBe(DEPLOYMENT_GAP_PROVENANCE);
      expect(record.source_system).toBe(DEPLOYMENT_SOURCE);
      expect(record.iteration).toBe(0);
    }
  });

  it("seeds nothing when every capability the roles need is bound", async () => {
    const started = await newLedger({ needs: ["telemetry_search"], tools: [SEARCH] });

    expect(gaps(started)).toEqual([]);
  });

  it("seeds nothing for an arch whose roles ask for no capability at all", async () => {
    expect(gaps(await newLedger())).toEqual([]);
  });

  // Declared once at start. A worker picking the run back up resumes the ledger,
  // and resumeHunt seeds nothing, so the gap cannot be restated on every handover.
  it("does not re-declare a gap the ledger already holds when the run is picked back up", async () => {
    const started = await newLedger({ needs: ["telemetry_search"] });
    const resumed = await reopen(started);

    expect([...resumed.projection.evidence.values()].filter(isGap)).toHaveLength(1);
  });

  it("tells the operator what the hunt will run without, at the checkpoint that starts it", async () => {
    const started = await newLedger({ needs: ["telemetry_search"] });

    const [checkpoint] = [...started.ledger.projection.checkpoints.values()];
    expect(checkpoint!.context!["unbound_capabilities"]).toEqual(["telemetry_search"]);
  });
});

describe("what a declared gap does to a verdict", () => {
  // Naming no hypothesis is not naming none. Nothing can answer these, so they
  // are a floor on open_gaps for every claim the hunt tries to settle.
  it("counts against every hypothesis, because no dispatch can ever close it", async () => {
    const started = await newLedger({ needs: ["telemetry_search"] });
    const [hypothesisId] = [...started.ledger.projection.hypotheses.keys()];

    expect(evidenceStrength(started.ledger.projection, hypothesisId!).open_gaps).toBe(1);
  });

  // One missing capability is worth reporting and not worth blocking on: the
  // hunt still says so, and evidence from elsewhere can still settle the claim.
  it("does not by itself gate a verdict when only one capability is missing", async () => {
    const started = await newLedger({ needs: ["telemetry_search"] });
    const [hypothesisId] = [...started.ledger.projection.hypotheses.keys()];
    provable(started.ledger, hypothesisId!);

    const strength = evidenceStrength(started.ledger.projection, hypothesisId!);
    expect(strength.open_gaps).toBeLessThan(DEFAULT_VERDICTS.gap_lock_threshold);
    expect(unmetPredicates(strength, DEFAULT_VERDICTS).join(" ")).not.toContain("visibility gap");
  });

  // ponytail: the ceiling is gap_lock_threshold, which is operator-settable. A
  // deployment missing three of four capabilities proves nothing, which is the
  // honest answer for it -- but it is a cliff, not a slope.
  it("gap-locks every verdict once as many capabilities are missing as the threshold allows", async () => {
    const started = await newLedger({
      needs: ["telemetry_search", "indicator_lookup", "findings_search"],
    });
    const [hypothesisId] = [...started.ledger.projection.hypotheses.keys()];
    provable(started.ledger, hypothesisId!);

    const strength = evidenceStrength(started.ledger.projection, hypothesisId!);
    expect(strength.open_gaps).toBe(DEFAULT_VERDICTS.gap_lock_threshold);
    expect(unmetPredicates(strength, DEFAULT_VERDICTS)).toContain("3 open visibility gap(s) bear on it");
  });

  // Through the verdict itself, not only the strength it reads: the hunt agent's
  // validate_hypothesis applies the same lock.
  it("keeps a well-supported hypothesis at its earned status rather than proven when the deployment is that blind", async () => {
    const started = await newLedger({
      needs: ["telemetry_search", "indicator_lookup", "findings_search"],
      checkpoints: { verdict_review: "auto" },
    });
    const [hypothesisId] = [...started.ledger.projection.hypotheses.keys()];
    provable(started.ledger, hypothesisId!);

    expect(validate(started.ledger, hypothesisId!, true).said).toContain("gap-locked");
    const hypothesis = started.ledger.projection.hypotheses.get(hypothesisId!)!;
    expect(hypothesis.status).toBe("active");
    expect(hypothesis.resolution_reason).toMatch(/could not finish: 3 sources unreachable/);
  });

  it("still lets a verdict land when one capability is missing and the rest corroborate", async () => {
    const started = await newLedger({ needs: ["telemetry_search"], checkpoints: { verdict_review: "auto" } });
    const [hypothesisId] = [...started.ledger.projection.hypotheses.keys()];
    provable(started.ledger, hypothesisId!);

    validate(started.ledger, hypothesisId!, true);
    expect(started.ledger.projection.hypotheses.get(hypothesisId!)!.status).toBe("proven");
  });

  it("earns no corroboration credit of its own: a missing tool corroborates nothing", async () => {
    const started = await newLedger({ needs: ["telemetry_search"] });
    const [hypothesisId] = [...started.ledger.projection.hypotheses.keys()];

    expect(evidenceStrength(started.ledger.projection, hypothesisId!).corroborating_sources).toBe(0);
  });
});

describe("the report says the hunt ran without it", () => {
  it("reports each one as unattributed under visibility gaps, saying what is missing", async () => {
    const started = await newLedger({ needs: ["telemetry_search"] });

    const report = buildReport(started.ledger.projection);
    expect(report.gaps).toHaveLength(1);
    expect(report.gaps[0]!.summary).toBe("No SIEM is connected, so the hunt could not search raw telemetry");
    expect(report.gaps[0]!.hypothesis_id).toBeNull();
  });
});

describe("unboundCapabilities", () => {
  it("names what the agent needs that no tool provides", () => {
    expect(unboundCapabilities(["telemetry_search", "findings_search"], [SEARCH])).toEqual(["findings_search"]);
  });

  it("counts a capability once however often it is asked for", () => {
    expect(unboundCapabilities(["telemetry_search", "telemetry_search"], [])).toEqual(["telemetry_search"]);
  });

  it("ignores a tool that declares no capability, so a bare grant binds nothing", () => {
    expect(unboundCapabilities(["telemetry_search"], [{ id: "expand", kind: "local" }])).toEqual(["telemetry_search"]);
  });

  // The gap the opening surfaces is read off what the plugin needs; it must name
  // every capability the retired worker roles asked for, or a missing one goes unsaid.
  // The hunt also opens single alerts (get_finding), which the workers never asked for.
  it.each([
    ["hunt", ["findings_search", "get_finding", "telemetry_search", "indicator_lookup", "entity_recall"]],
    ["adjudicate", ["findings_search", "telemetry_search", "indicator_lookup", "entity_recall"]],
  ])("reads %s's needs as every capability its querying asks for", (id, expected) => {
    expect(unboundCapabilities(pluginFor(id).needs, [])).toEqual(expected);
  });
});

// A ledger written before the gap was worded still reads in words: the capability is on its payload.
describe("an older ledger's deployment gap", () => {
  it("reads in words on the evidence row a console is sent", async () => {
    const started = await newLedger();
    started.ledger.append({
      kind: "evidence",
      payload: {
        evidence_id: "ev-old",
        dispatch_id: null,
        iteration: 0,
        source_system: DEPLOYMENT_SOURCE,
        summary: "no tool in this deployment answers telemetry_search",
        payload: { capability: "telemetry_search", hypothesis_id: null },
        salience: "notable",
        why_notable: "the roles that need it run without it, so no query can close this",
        provenance: DEPLOYMENT_GAP_PROVENANCE,
        attacker_influenceable: false,
        instruction_like: false,
        entities: [],
        captured_at: new Date().toISOString(),
      },
    });
    await started.ledger.flush();

    const [row] = huntProjection("run", await started.state.read(started.runId)).evidence;
    expect(row).toMatchObject({ is_gap: true, summary: "No SIEM is connected, so the hunt could not search raw telemetry" });
  });
});
