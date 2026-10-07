import { describe, expect, it } from "vitest";
import { unboundCapabilities, type Roles, type ToolSpec } from "../../core/spec.js";
import { DEFAULT_VERDICTS } from "../../workflows/hunt/config.js";
import { DEPLOYMENT_SOURCE } from "../../workflows/hunt/controller.js";
import { buildReport } from "../../workflows/hunt/report.js";
import { DEPLOYMENT_GAP_PROVENANCE, evidenceStrength, isGap, unmetPredicates } from "../../workflows/hunt/strength.js";
import { newLedger, provable, reopen, type Started } from "../support/hunt.js";

const SEARCH: ToolSpec = { id: "elastic_elastic_search_logs", kind: "remote", provides: "telemetry_search" };

const gaps = (started: Started) => [...started.ledger.projection.evidence.values()].filter(isGap);
const summaries = (started: Started) => gaps(started).map((record) => record.summary);

describe("a capability nothing provides is a blind spot the hunt declares", () => {
  it("seeds one gap per capability no tool in this deployment answers", async () => {
    const started = await newLedger({ needs: ["telemetry_search", "indicator_lookup"] });

    expect(summaries(started)).toEqual([
      "no tool in this deployment answers telemetry_search",
      "no tool in this deployment answers indicator_lookup",
    ]);
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

  it("earns no corroboration credit of its own: a missing tool corroborates nothing", async () => {
    const started = await newLedger({ needs: ["telemetry_search"] });
    const [hypothesisId] = [...started.ledger.projection.hypotheses.keys()];

    expect(evidenceStrength(started.ledger.projection, hypothesisId!).corroborating_sources).toBe(0);
  });
});

describe("the report says the hunt ran without it", () => {
  it("reports each one as unattributed under visibility gaps, naming the capability", async () => {
    const started = await newLedger({ needs: ["telemetry_search"] });

    const report = buildReport(started.ledger.projection);
    expect(report.gaps).toHaveLength(1);
    expect(report.gaps[0]!.summary).toBe("no tool in this deployment answers telemetry_search");
    expect(report.gaps[0]!.hypothesis_id).toBeNull();
  });
});

describe("unboundCapabilities", () => {
  const role = (needs: string[]) => ({ prompt: "", description: "", output_schema: {}, tools: [], needs });

  it("names what the roles asked for that no tool provides", () => {
    const roles: Roles = { lead: role(["telemetry_search"]), workers: { w: role(["findings_search"]) } };

    expect(unboundCapabilities(roles, [SEARCH])).toEqual(["findings_search"]);
  });

  it("counts a capability once however many roles ask for it", () => {
    const roles: Roles = { workers: { a: role(["telemetry_search"]), b: role(["telemetry_search"]) } };

    expect(unboundCapabilities(roles, [])).toEqual(["telemetry_search"]);
  });

  it("ignores a tool that declares no capability, so a bare grant binds nothing", () => {
    const roles: Roles = { workers: { a: role(["telemetry_search"]) } };

    expect(unboundCapabilities(roles, [{ id: "expand", kind: "local" }])).toEqual(["telemetry_search"]);
  });
});
