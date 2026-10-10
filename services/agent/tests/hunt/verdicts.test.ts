import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { archFor } from "../../plugins/kinds.js";
import { ZERO_TOKENS } from "../../contracts/budget.js";
import type { ToolResult } from "../../contracts/tool.js";
import { budgetOf, unmeteredQuota } from "../../core/budget.js";
import { nullMemory } from "../../core/memory.js";
import type { Message, Provider, ProviderEvent, TurnRequest } from "../../core/provider.js";
import { registryOf } from "../../core/registry.js";
import type { ToolDispatch } from "../../core/seams.js";
import { buildSpec, type RunSpec } from "../../core/spec.js";
import { InProcessState } from "../../core/state.js";
import {
  DEFAULT_VERDICTS,
  validateThresholds,
  verdictsOf,
} from "../../plugins/hunt/config.js";
import { InProcessDirectiveQueue } from "../../plugins/hunt/directives.js";
import { fold, type HuntEvent, type HuntKinds } from "../../plugins/hunt/ledger.js";
import { newId } from "../../plugins/hunt/ids.js";
import { startHunt } from "../../plugins/hunt/opening.js";
import { evidenceStrength, isGap } from "../../plugins/hunt/strength.js";
import { runHunt } from "../../plugins/hunt/workflow.js";
import {
  bookOf,
  evidenceOn,
  gapOn,
  huntSpecFor,
  newLedger,
  nullCheck,
  nullChecks,
  validate,
} from "../support/hunt.js";

async function oneHypothesis() {
  const { ledger, hypothesisIds } = await newLedger();
  return { ledger, hypothesisId: hypothesisIds[0]! };
}

describe("argue the null", () => {
  it("blocks proven when the benign explanation stands, and files it as counter-evidence", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    evidenceOn(ledger, hypothesisId, { source: "cloudtrail" });
    evidenceOn(ledger, hypothesisId, { source: "duckdb" });

    const result = validate(ledger, hypothesisId, false);

    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
    expect(result.said).toMatch(/benign explanation was not ruled out/);

    // The critic's argument is on the record like any other evidence, weakening
    // the hypothesis rather than deciding it.
    const record = nullChecks(ledger)[0]!;
    expect(record.source_system).toBe("critic");
    expect(record.salience).toBe("notable");
    expect(ledger.projection.links).toContainEqual({
      evidence_id: record.evidence_id,
      hypothesis_id: hypothesisId,
      relation: "weakens",
    });
  });

  it("proves a hypothesis that survives it on two source systems", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    evidenceOn(ledger, hypothesisId, { source: "cloudtrail" });
    evidenceOn(ledger, hypothesisId, { source: "duckdb" });

    const result = validate(ledger, hypothesisId, true);

    const hypothesis = ledger.projection.hypotheses.get(hypothesisId)!;
    expect(hypothesis.status).toBe("proven");
    expect(hypothesis.resolution_reason).toMatch(/survived the argue-the-null pass/);
    expect(result.said).toBe(`${hypothesisId} proven`);

    // The snapshot is what the numbers were at verdict time, so the verdict can
    // be re-read later without recomputing it against a moved ledger.
    expect(hypothesis.evidence_strength).toEqual({
      corroborating_sources: 2,
      contradicting_records: 0,
      open_gaps: 0,
      attacker_influenceable_only: false,
      survived_disconfirmation: true,
    });

    // A surviving null check is not itself support: it must not add a source.
    expect(nullChecks(ledger)).toHaveLength(1);
    expect(ledger.projection.links.filter((link) => link.relation === "supports")).toHaveLength(2);
  });

  it("hands the critic raw payloads rather than summaries", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    evidenceOn(ledger, hypothesisId, { source: "cloudtrail" });

    const check = bookOf(ledger).nullCheckInput(ledger.projection.hypotheses.get(hypothesisId)!);

    expect(check.hypothesis_id).toBe(hypothesisId);
    expect(check.statement).toContain("a credential is used");
    expect(check.evidence[0]!.relation).toBe("supports");
    expect(check.evidence[0]!.record.payload).toEqual({ rows: 3, src_ip: "45.77.53.176" });
  });
});

describe("evidence_strength predicates", () => {
  it("refuses proven on a single corroborating source", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    evidenceOn(ledger, hypothesisId, { source: "cloudtrail" });

    const result = validate(ledger, hypothesisId, true);

    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
    expect(result.said).toMatch(/1 corroborating source system\(s\), 2 required/);
  });

  it("counts two records from the same source system as one", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    evidenceOn(ledger, hypothesisId, { source: "cloudtrail" });
    evidenceOn(ledger, hypothesisId, { source: "cloudtrail" });

    validate(ledger, hypothesisId, true);

    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
    expect(evidenceStrength(ledger.projection, hypothesisId).corroborating_sources).toBe(1);
  });

  it("refuses proven when nothing supporting it was attested by the telemetry", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    evidenceOn(ledger, hypothesisId, { source: "http", restsOn: [{ field: "filename", authored: "adversary" }] });
    evidenceOn(ledger, hypothesisId, { source: "dns", restsOn: [{ field: "threat_label", authored: "third_party" }] });

    const result = validate(ledger, hypothesisId, true);

    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
    expect(result.said).toMatch(/the telemetry attested/);
  });

  // Attacker-caused is not attacker-authored: the adversary chose the destination and
  // the interval, and the sensor counted the connections. A hunt whose every record is
  // flagged attacker-influenceable could not prove anything at any budget.
  it("allows proven when the support rests on what the sensor counted", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    evidenceOn(ledger, hypothesisId, {
      source: "net_flow",
      attackerInfluenceable: true,
      restsOn: [{ field: "conn_count", authored: "sensor" }, { field: "dest_ip", authored: "adversary" }],
    });
    evidenceOn(ledger, hypothesisId, {
      source: "endpoint",
      attackerInfluenceable: true,
      restsOn: [{ field: "parent_process", authored: "sensor" }],
    });

    validate(ledger, hypothesisId, true);

    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("proven");
  });

  // stated_confidence rode on the lead's VALIDATE decision; the agent's
  // validate_hypothesis takes only an id, so there is no confidence to gate on.

  it("attributes a gap to the hypothesis whose dispatch went unanswered", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    gapOn(ledger, hypothesisId);
    gapOn(ledger, null);

    expect(evidenceStrength(ledger.projection, hypothesisId).open_gaps).toBe(1);
    expect([...ledger.projection.evidence.values()].filter(isGap)).toHaveLength(2);
  });

  it("counts a repeated failure of one query as one gap, not three", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    for (let attempt = 0; attempt < 3; attempt += 1) gapOn(ledger, hypothesisId, "the same question");

    // Otherwise a flaky tool blinds a hypothesis it was only slow to answer.
    expect(evidenceStrength(ledger.projection, hypothesisId).open_gaps).toBe(1);
  });
});

describe("the argument has to be current", () => {
  it("refuses proven when the latest argument stands, whatever an earlier one said", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    evidenceOn(ledger, hypothesisId, { source: "cloudtrail" });

    // Survives, but on one source, so it stays up for a second look.
    validate(ledger, hypothesisId, true);
    evidenceOn(ledger, hypothesisId, { source: "duckdb" });
    const second = validate(ledger, hypothesisId, false);

    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
    expect(second.said).toMatch(/benign explanation was not ruled out/);
    expect(evidenceStrength(ledger.projection, hypothesisId).survived_disconfirmation).toBe(false);
  });

  it("records what the critic was shown, so the verdict can be re-read", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    const citations = [evidenceOn(ledger, hypothesisId, { source: "cloudtrail" })];

    validate(ledger, hypothesisId, true);

    expect(nullChecks(ledger)[0]!.payload["argued_evidence_ids"]).toEqual(citations);
  });

  it("does not let a survival argued over less carry support added since", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    const first = evidenceOn(ledger, hypothesisId, { source: "cloudtrail" });
    evidenceOn(ledger, hypothesisId, { source: "duckdb" });

    // The critic was shown only the first record; the second it never argued against.
    bookOf(ledger).applyVerdict(hypothesisId, nullCheck(true), [first]);

    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
    expect(evidenceStrength(ledger.projection, hypothesisId).survived_disconfirmation).toBe(false);
  });
});

describe("gap lock", () => {
  it("leaves a blinded hypothesis at its earned status, never proven or disproven", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    evidenceOn(ledger, hypothesisId, { source: "cloudtrail" });
    evidenceOn(ledger, hypothesisId, { source: "duckdb" });
    // Everything else clears, so only the gaps can be what stopped it. Three
    // distinct questions, because a retry of one is one blind spot.
    for (let gap = 0; gap < DEFAULT_VERDICTS.gap_lock_threshold; gap += 1) gapOn(ledger, hypothesisId);

    const result = validate(ledger, hypothesisId, true);

    const hypothesis = ledger.projection.hypotheses.get(hypothesisId)!;
    expect(hypothesis.status).toBe("active");
    expect(hypothesis.resolution_reason).toMatch(/could not finish: 3 sources unreachable/);
    expect(hypothesis.evidence_strength!.open_gaps).toBe(DEFAULT_VERDICTS.gap_lock_threshold);
    expect(result.said).toMatch(/gap-locked/);
  });

  it("locks whichever way the critic argued", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    evidenceOn(ledger, hypothesisId, { source: "cloudtrail" });
    for (let gap = 0; gap < DEFAULT_VERDICTS.gap_lock_threshold; gap += 1) gapOn(ledger, hypothesisId);

    validate(ledger, hypothesisId, false);

    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("active");
  });

  // Gaps from the agent's own calls count, one per tool, on the hypothesis they name.
  it("counts a call the estate could not answer, as the agent hit it", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    const book = bookOf(ledger);
    for (const tool of ["splunk_search", "okta_logs", "edr_query"]) book.appendGap(tool, "unavailable", hypothesisId);

    expect(evidenceStrength(ledger.projection, hypothesisId).open_gaps).toBe(DEFAULT_VERDICTS.gap_lock_threshold);
  });
});

describe("the verdict is the only writer", () => {
  it("leaves a proven hypothesis alone when the hunt ends", async () => {
    const { ledger, hypothesisId } = await oneHypothesis();
    evidenceOn(ledger, hypothesisId, { source: "cloudtrail" });
    evidenceOn(ledger, hypothesisId, { source: "duckdb" });

    validate(ledger, hypothesisId, true);
    expect(bookOf(ledger).conclude("done").concluded).toBe(true);

    expect(ledger.projection.hunt.outcome).toBe("completed");
    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("proven");
  });

  // A kernel hunt always has its critic: validate_hypothesis builds it off the
  // harness, so "no critic configured" is not a state the hunt can be in.

  // The tool's hypothesis_id is required by its schema, and an unknown one is refused
  // below rather than re-asked as the controller's VALIDATE did.
});

// The critic is the agent's harness's own model call, so a failing one is driven
// through a whole run rather than a double.
describe("the critic as a model call", () => {
  const FIXTURES = join(import.meta.dirname, "..", "fixtures");

  function specOf(): RunSpec {
    const entry = archFor("hunt");
    const spec = buildSpec({ arch: entry.arch, playbook: join(FIXTURES, "hunt.playbook.yaml"), config: join(FIXTURES, "hunt.config.yaml") }, entry.actions, entry.owned);
    return { ...spec, sections: { ...spec.sections, hypotheses: ["10.0.0.5 is beaconing to 45.77.53.176"] } };
  }

  function hypothesisIn(request: TurnRequest): string {
    const text = request.messages.map((message: Message) => message.content).join("\n");
    const found = /- (h-[0-9a-f]+) \[active\]/.exec(text);
    if (found === null) throw new Error("no hypothesis on the board");
    return found[1] as string;
  }

  type Step = (request: TurnRequest) => { tool: string; args: unknown };

  // Calls the steps in order; asked for the critic's argument, it fails.
  function modelOf(steps: readonly Step[], critic: () => never): Provider {
    let at = 0;
    return {
      model: "scripted/model",
      provider_type: "scripted",
      stream: async function* (request: TurnRequest): AsyncGenerator<ProviderEvent> {
        if (request.emit !== undefined) {
          if ("benign_explanation_stands" in ((request.emit["properties"] ?? {}) as Record<string, unknown>)) critic();
          yield { type: "text_delta", text: JSON.stringify({ summary: "s", what_happened: "w", next_steps: [] }) };
          yield { type: "usage", tokens: ZERO_TOKENS };
          return;
        }
        const step = request.tools.length === 0 ? undefined : steps[at];
        at += 1;
        yield { type: "usage", tokens: ZERO_TOKENS };
        if (step === undefined) return;
        const next = step(request);
        yield { type: "tool_call", call: { id: `c${at}`, tool: next.tool, args: JSON.stringify(next.args) } };
      },
    };
  }

  const estate: ToolDispatch = {
    invoke: async (tool, args): Promise<ToolResult> => tool.invoke(args),
  };

  const finding = (source: string, hypothesis: string) => ({
    source_system: source,
    summary: `412 connections from 10.0.0.5 to 45.77.53.176 (${source})`,
    salience: "anomalous",
    why_notable: "too regular for a person",
    payload: JSON.stringify({ connections: 412 }),
    supports: [hypothesis],
    rests_on: [{ field: "connections", authored: "sensor" }],
  });

  async function run(steps: Step[], critic: () => never): Promise<readonly HuntEvent[]> {
    const state = new InProcessState<HuntKinds>();
    const runId = newId("run");
    const spec = specOf();
    await runHunt(
      {
        provider: modelOf(steps, critic),
        registry: registryOf([], {}),
        dispatch: estate,
        budget: budgetOf({ ...spec.budgets, max_calls: 40 }, unmeteredQuota),
        memory: nullMemory,
        state,
      },
      { run_id: runId, run_kind: "hunt", spec, actions: [], queue: new InProcessDirectiveQueue() },
    );
    return (await state.read(runId)) as readonly HuntEvent[];
  }

  const validateCall = (events: readonly HuntEvent[]) =>
    events.find((event) => event.kind === "call" && (event.payload as { tool: string }).tool === "validate_hypothesis")?.payload as
      | { ok: boolean; result: string }
      | undefined;

  // An argument that could not run is not one the hypothesis survived: the verdict is refused.
  it("keeps the hypothesis open when the critic itself fails", async () => {
    const events = await run(
      [
        (request) => ({ tool: "record_evidence", args: { results: [finding("network", hypothesisIn(request)), finding("endpoint", hypothesisIn(request))] } }),
        (request) => ({ tool: "validate_hypothesis", args: { hypothesis_id: hypothesisIn(request) } }),
      ],
      () => {
        throw new Error("gateway unreachable");
      },
    );

    // An argument that could not run is not an argument that was won.
    expect([...fold(events).hypotheses.values()][0]?.status).not.toBe("proven");
    expect(validateCall(events)?.ok).toBe(false);
  });

  it("refuses a verdict on a hypothesis the ledger does not hold", async () => {
    const events = await run([() => ({ tool: "validate_hypothesis", args: { hypothesis_id: "h-nope" } })], () => {
      throw new Error("the critic is never reached");
    });

    expect(validateCall(events)?.ok).toBe(false);
    expect(validateCall(events)?.result).toMatch(/h-nope is not on the ledger/);
    expect(events.some((event) => event.kind === "evidence" && (event.payload as { provenance: string }).provenance === "null_check")).toBe(false);
  });
});

// What the critic costs reaches the hunt through the spend events every model call
// journals; the controller's per-decision cost record went with the decision.

describe("verdict thresholds are config", () => {
  it("ships the documented defaults and honours an override", () => {
    expect(verdictsOf(huntSpecFor())).toEqual(DEFAULT_VERDICTS);
    expect(verdictsOf({ ...huntSpecFor(), thresholds: { min_corroborating_sources: 1 } })).toEqual({
      ...DEFAULT_VERDICTS,
      min_corroborating_sources: 1,
    });
  });

  it("refuses a threshold that would prove everything or lock everything", () => {
    expect(() => validateThresholds({ gap_lock_threshold: 0 })).toThrow(/must be a positive number/);
    expect(() => validateThresholds({ min_sources: 2 })).toThrow(/unknown thresholds key/);
  });

  it("proves on one source when the deployment says one source is enough", async () => {
    // Read off the journaled spec, so a resumed hunt keeps the bar it started under.
    const state = new InProcessState<HuntKinds>();
    const ledger = await startHunt(state, new InProcessDirectiveQueue(), newId("run"), {
      ...huntSpecFor(),
      thresholds: { min_corroborating_sources: 1, gap_lock_threshold: 3 },
    });
    const hypothesisId = [...ledger.projection.hypotheses.keys()][0]!;
    evidenceOn(ledger, hypothesisId, { source: "cloudtrail" });

    validate(ledger, hypothesisId, true);

    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).toBe("proven");
  });
});
