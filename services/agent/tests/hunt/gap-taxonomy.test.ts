// CONTEXT.md, Visibility Gap: a tool that timed out or was unavailable. "A refusal or a
// bad argument is a defect and must never be recorded as one." The hunt decided gaps by
// dispatch shape instead, so a worker that could not format its answer, or one parked on
// an approval, was recorded as a blind spot -- floored to anomalous, and three on distinct
// leads gap-locked a hypothesis to inconclusive as though the hunt could not look (#1436).
import { describe, expect, it } from "vitest";
import { budgetOf, unmeteredQuota } from "../../core/budget.js";
import { defineTool, type ToolResult } from "../../contracts/tool.js";
import { localDispatch } from "../../core/dispatch.js";
import type { Harness } from "../../core/loop.js";
import { nullMemory } from "../../core/memory.js";
import { registryOf } from "../../core/registry.js";
import type { RunSpec } from "../../core/spec.js";
import { InProcessState } from "../../core/state.js";
import { workerDispatcher } from "../../workflows/hunt/adapters.js";
import { DEFAULT_VERDICTS } from "../../workflows/hunt/config.js";
import type { HuntKinds } from "../../workflows/hunt/ledger.js";
import { ScriptedDisconfirmationCritic } from "../../workflows/hunt/scripted.js";
import { openGaps } from "../../workflows/hunt/strength.js";
import type { Decision } from "../../workflows/hunt/types.js";
import { scriptedProvider, type ScriptedTurn } from "../support/scripted-provider.js";
import { controllerFor, evidenceOn, newLedger, validateOn } from "../support/hunt.js";

const SEARCH = defineTool(
  {
    id: "splunk_execute",
    description: "run SPL",
    parameters: { type: "object", properties: { spl_query: { type: "string" } } },
    execute: async () => ({ ok: true as const, rows: [{ n: 1 }], rowCount: 1, capped: false, sourceSystem: "cisco:asa" }),
  },
  { maxRows: 10, timeoutMs: 1_000 },
  true,
);

// What the tool does this dispatch: a hang that outlives timeoutMs, or an instant answer.
let behaviour: "hang" | "ok" | "invalid_args" = "ok";

const FLAKY = defineTool(
  {
    id: "flaky_search",
    description: "search that may time out",
    parameters: { type: "object", properties: {} },
    execute: (_args, _bounds, signal): Promise<ToolResult> => {
      if (behaviour === "invalid_args") return Promise.resolve({ ok: false, failure: { kind: "invalid_args", detail: "bad spl" } });
      if (behaviour === "ok") return Promise.resolve({ ok: true, rows: [{ n: 1 }], rowCount: 1, capped: false, sourceSystem: "cisco:asa" });
      return new Promise((_resolve, reject) => signal.addEventListener("abort", () => reject(new Error("aborted"))));
    },
  },
  { maxRows: 10, timeoutMs: 20 },
  true,
);

function specWith(approvals: string[] = []): RunSpec {
  return {
    arch: "threathunt",
    approvals,
    runtime: { max_turns: 4, result_cap: 8_000, recall_limit: 0 },
    roles: {
      workers: {
        threat_hunter: {
          prompt: "look",
          description: "hunts",
          output_schema: { type: "object", required: ["evidence"], properties: { evidence: { type: "array" } } },
          tools: [],
          needs: [],
        },
      },
    },
  } as unknown as RunSpec;
}

function harnessFor(script: readonly ScriptedTurn[]): Harness<HuntKinds> {
  return {
    provider: scriptedProvider(script),
    registry: registryOf([SEARCH, FLAKY], { threat_hunter: ["splunk_execute", "flaky_search"] }),
    dispatch: localDispatch,
    budget: budgetOf({ max_calls: 100, max_cost_usd: 5, max_wall_ms: 600_000, max_park_ms: 1_000 }, unmeteredQuota),
    memory: nullMemory,
    state: new InProcessState(),
  } as unknown as Harness<HuntKinds>;
}

// One worker turn that answers nothing, then two emissions the schema rejects: the
// exact shape of stream.ts giving up with "the role never emitted a valid answer".
const MALFORMED: readonly ScriptedTurn[] = [
  { content: "looked" },
  { emit: "not json at all" },
  { emit: "still not json" },
];

const REQUEST = {
  dispatch_id: "dsp-1",
  agent_id: "threat_hunter",
  query_intent: "find beaconing",
  focus: "",
  target_hypothesis_id: null,
  scope: {},
} as never;

describe("a worker defect is not a visibility gap", () => {
  it("names an emission the schema rejected twice as our stop, not the estate's", async () => {
    const dispatcher = workerDispatcher({
      harness: harnessFor(MALFORMED),
      spec: specWith(),
      run_id: "run-1",
      run_kind: "hunt",
      actions: [],
      recall_keys: [],
    });

    const result = await dispatcher.dispatch(REQUEST);

    expect(result.failed).toBe(true);
    expect(result.failure_reason).toMatch(/never emitted a valid answer/);
    expect(result.stopped_by).toBe("emission_invalid");
  });

  // Latent while the shipped hunt config grants no gated tool, and wrong the day one is.
  it("names a worker parked on an approval as our stop, not the estate's", async () => {
    const dispatcher = workerDispatcher({
      harness: harnessFor([{ calls: [{ tool: "splunk_execute", args: '{"spl_query":"index=botsv3"}' }] }]),
      spec: specWith(["splunk_execute"]),
      run_id: "run-1",
      run_kind: "hunt",
      actions: [],
      recall_keys: [],
    });

    const result = await dispatcher.dispatch(REQUEST);

    expect(result.failed).toBe(true);
    expect(result.failure_reason).toMatch(/parked on approval for splunk_execute/);
    expect(result.stopped_by).toBe("parked");
  });

  it("writes no gap record for a worker that emitted invalid JSON twice", async () => {
    const { ledger, hypothesisIds } = await newLedger();
    const hypothesisId = hypothesisIds[0]!;
    const dispatcher = workerDispatcher({
      harness: harnessFor(MALFORMED),
      spec: specWith(),
      run_id: "run-1",
      run_kind: "hunt",
      actions: [],
      recall_keys: [],
    });
    const investigate: Decision = {
      action: "INVESTIGATE",
      rationale: "look",
      query_intent: "find beaconing",
      target_hypothesis_id: hypothesisId,
    };

    await controllerFor(ledger, [investigate], { dispatcher }).advanceIteration();

    const [dispatch] = [...ledger.projection.dispatches.values()];
    expect(dispatch!.status).toBe("failed");
    expect(dispatch!.stopped_by).toBe("emission_invalid");
    const gaps = [...ledger.projection.evidence.values()].filter((record) => record.provenance === "tool_failure");
    expect(gaps).toEqual([]);
    expect(openGaps(ledger.projection, hypothesisId)).toBe(0);
  });

  // Issue #1436 case D: three malformed workers on distinct leads used to close a
  // well-supported hypothesis as "gap-locked ... the hunt could not look".
  it("does not gap-lock a hypothesis over three malformed workers on distinct leads", async () => {
    const { ledger, hypothesisIds } = await newLedger();
    const hypothesisId = hypothesisIds[0]!;
    const citations = [
      evidenceOn(ledger, hypothesisId, { source: "cloudtrail" }),
      evidenceOn(ledger, hypothesisId, { source: "duckdb" }),
    ];
    const leads = Array.from({ length: DEFAULT_VERDICTS.gap_lock_threshold }, (_, index) => index);
    const dispatcher = workerDispatcher({
      harness: harnessFor(leads.flatMap(() => MALFORMED)),
      spec: specWith(),
      run_id: "run-1",
      run_kind: "hunt",
      actions: [],
      recall_keys: [],
    });
    const investigations: Decision[] = leads.map((index) => ({
      action: "INVESTIGATE",
      rationale: "look",
      query_intent: `distinct lead ${index}`,
      target_hypothesis_id: hypothesisId,
    }));

    const controller = controllerFor(ledger, [...investigations, validateOn(hypothesisId, citations)], {
      dispatcher,
      critic: new ScriptedDisconfirmationCritic(true),
    });
    for (let step = 0; step <= leads.length; step += 1) await controller.advanceIteration();

    const failed = [...ledger.projection.dispatches.values()].filter((one) => one.status === "failed");
    expect(failed).toHaveLength(DEFAULT_VERDICTS.gap_lock_threshold);
    expect(failed.every((one) => one.stopped_by === "emission_invalid")).toBe(true);
    expect(openGaps(ledger.projection, hypothesisId)).toBe(0);

    const hypothesis = ledger.projection.hypotheses.get(hypothesisId)!;
    // Two independent sources and a critic that could not stand the benign case up:
    // with no blind spot in the way, that is proven.
    expect(hypothesis.status).toBe("proven");
    expect(hypothesis.resolution_reason ?? "").not.toMatch(/gap-locked|could not look/);
  });
});

// Issue #1436 case A: the worker's tool timed out, the worker answered anyway. The
// dispatch is complete, but what it concluded rests on data the hunt never saw.
const ANSWERED: readonly ScriptedTurn[] = [
  { calls: [{ tool: "flaky_search", args: "{}" }] },
  { content: "searched" },
  { emit: { evidence: [], results: [] } },
];

describe("a tool the estate could not answer is a visibility gap even when the worker answers", () => {
  const dispatcherFor = (script: readonly ScriptedTurn[]) =>
    workerDispatcher({ harness: harnessFor(script), spec: specWith(), run_id: "run-1", run_kind: "hunt", actions: [], recall_keys: [] });
  const investigate = (hypothesisId: string, intent: string): Decision => ({
    action: "INVESTIGATE",
    rationale: "look",
    query_intent: intent,
    target_hypothesis_id: hypothesisId,
  });
  const gapRecords = (ledger: Awaited<ReturnType<typeof newLedger>>["ledger"]) =>
    [...ledger.projection.evidence.values()].filter((record) => record.provenance === "tool_failure");

  it("surfaces a timeout on a completed dispatch, once per tool and kind", async () => {
    behaviour = "hang";
    const result = await dispatcherFor(ANSWERED).dispatch(REQUEST);

    expect(result.failed).toBe(false);
    expect(result.tool_gaps).toEqual([{ tool: "flaky_search", kind: "timeout" }]);
  });

  it("holds the gap open until a later completed dispatch on the same lead has none", async () => {
    const { ledger, hypothesisIds } = await newLedger();
    const hypothesisId = hypothesisIds[0]!;
    const dispatcher = dispatcherFor([...ANSWERED, ...ANSWERED]);
    const controller = controllerFor(ledger, [investigate(hypothesisId, "same lead"), investigate(hypothesisId, "same lead")], { dispatcher });

    behaviour = "hang";
    await controller.advanceIteration();

    expect([...ledger.projection.dispatches.values()].map((one) => one.status)).toEqual(["complete"]);
    expect(gapRecords(ledger)).toHaveLength(1);
    expect(gapRecords(ledger)[0]!.payload).toMatchObject({ tool: "flaky_search", kind: "timeout", gap_key: "same lead" });
    expect(openGaps(ledger.projection, hypothesisId)).toBe(1);

    behaviour = "ok";
    await controller.advanceIteration();
    expect(openGaps(ledger.projection, hypothesisId)).toBe(0);
  });

  it("records no gap for a call that was refused or malformed and still answered", async () => {
    behaviour = "invalid_args";
    const { ledger, hypothesisIds } = await newLedger();
    const hypothesisId = hypothesisIds[0]!;
    const controller = controllerFor(ledger, [investigate(hypothesisId, "bad query")], { dispatcher: dispatcherFor(ANSWERED) });

    await controller.advanceIteration();

    expect(gapRecords(ledger)).toEqual([]);
    expect(openGaps(ledger.projection, hypothesisId)).toBe(0);
  });

  it("does not prove a hypothesis over three leads whose sources timed out", async () => {
    behaviour = "hang";
    const { ledger, hypothesisIds } = await newLedger();
    const hypothesisId = hypothesisIds[0]!;
    const citations = [
      evidenceOn(ledger, hypothesisId, { source: "cloudtrail" }),
      evidenceOn(ledger, hypothesisId, { source: "duckdb" }),
    ];
    const leads = Array.from({ length: DEFAULT_VERDICTS.gap_lock_threshold }, (_, index) => index);
    const controller = controllerFor(
      ledger,
      [...leads.map((index) => investigate(hypothesisId, `distinct lead ${index}`)), validateOn(hypothesisId, citations)],
      { dispatcher: dispatcherFor(leads.flatMap(() => ANSWERED)), critic: new ScriptedDisconfirmationCritic(true) },
    );
    for (let step = 0; step <= leads.length; step += 1) await controller.advanceIteration();

    expect(openGaps(ledger.projection, hypothesisId)).toBe(DEFAULT_VERDICTS.gap_lock_threshold);
    expect(ledger.projection.hypotheses.get(hypothesisId)!.status).not.toBe("proven");
  });
});
