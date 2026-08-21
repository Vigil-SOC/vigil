// The execution log: a summary is the worker's account of the data, a call is the
// query that produced it. Journaled empty on every dispatch until #0017, which made
// the "reproducible query set" unreachable rather than deferred.
import { describe, expect, it } from "vitest";
import type { Attempt } from "../../core/loop.js";
import { wrap, scannerFor } from "../../core/security.js";
import { callsOf, charged, link, salvaged, workerDispatcher } from "../../workflows/hunt/adapters.js";
import { getEventListeners, getMaxListeners } from "node:events";
import { narrativeOf, renderDispatch, renderNullCheck } from "../../workflows/hunt/render.js";
import { budgetOf, unmeteredQuota } from "../../core/budget.js";
import { defineTool } from "../../contracts/tool.js";
import { localDispatch } from "../../core/dispatch.js";
import type { Harness } from "../../core/loop.js";
import { nullMemory } from "../../core/memory.js";
import { registryOf } from "../../core/registry.js";
import type { RunSpec } from "../../core/spec.js";
import { InProcessState } from "../../core/state.js";
import type { HuntKinds } from "../../workflows/hunt/ledger.js";
import { scriptedProvider } from "../support/scripted-provider.js";
import { renderCaseFile } from "../../workflows/hunt/report.js";
import { newLedger, relate } from "../support/hunt.js";
import { newId } from "../../workflows/hunt/ids.js";

const attempt = (tool: string, args: string, rows: readonly unknown[]): Attempt => {
  const result = { ok: true as const, rows, rowCount: rows.length, capped: false, sourceSystem: "duckdb" };
  return { tool, args, result, wrapped: wrap(tool, result, scannerFor([]), 8_000) };
};

describe("the calls a dispatch ran", () => {
  it("journals the tool and the arguments an analyst would re-run", () => {
    const [call] = callsOf([attempt("telemetry_search", '{"query":"index=botsv3 dest_ip=45.77.53.176"}', [{ n: 1 }])]);

    expect(call!.tool).toBe("telemetry_search");
    expect(call!.arguments).toContain("index=botsv3");
    expect(call!.result).toContain("1 row(s) from duckdb");
  });

  it("shares one budget across the calls, so a large answer cannot crowd out the rest", () => {
    const big = Array.from({ length: 400 }, (_, n) => ({ n, filler: "x".repeat(200) }));
    const calls = callsOf([attempt("a", "{}", big), attempt("b", "{}", big), attempt("c", "{}", [{ n: 1 }])]);

    expect(calls).toHaveLength(3);
    expect(calls.every((call) => call.result.length <= 16_000 / 3 + 64)).toBe(true);
    // Truncation is marked, never silent -- output that just stops reads as short.
    expect(calls[0]!.result).toContain("[truncated");
    expect(calls[2]!.tool).toBe("c");
  });

  it("has nothing to say about a worker that ran no tools", () => {
    expect(callsOf([])).toEqual([]);
  });
});

describe("the case file a responder is handed", () => {
  it("carries the query behind the claim, not only the claim", async () => {
    const { ledger, hypothesisIds } = await newLedger();
    const hypothesisId = hypothesisIds[0]!;
    const dispatchId = newId("dsp");
    const evidenceId = newId("ev");

    ledger.append({
      kind: "dispatch",
      payload: {
        dispatch_id: dispatchId,
        iteration: 1,
        agent_id: "network_analyst",
        status: "complete",
        query_intent: "characterise the beaconing interval",
        target_hypothesis_id: hypothesisId,
        question_id: null,
        failure_reason: null,
        cost_usd: 0.2,
        calls: callsOf([attempt("telemetry_search", '{"query":"stats count by dest_ip"}', [{ dest_ip: "45.77.53.176" }])]),
      },
    } as never);
    ledger.append({
      kind: "evidence",
      payload: {
        evidence_id: evidenceId,
        dispatch_id: dispatchId,
        iteration: 1,
        source_system: "duckdb",
        summary: "412 connections every 300s +/- 4s",
        payload: { interval_s: 300 },
        salience: "anomalous",
        why_notable: "low-jitter periodicity",
        provenance: "worker",
        attacker_influenceable: false,
        instruction_like: false,
        entities: [],
        captured_at: new Date().toISOString(),
      },
    } as never);
    relate(ledger, evidenceId, hypothesisId, "supports");

    const rendered = renderCaseFile(ledger.projection, {
      case_id: "case-1",
      hypothesis_id: hypothesisId,
      iteration: 1,
      rationale: "proven and active",
      created_at: new Date().toISOString(),
    });

    expect(rendered).toContain("Queries behind it:");
    expect(rendered).toContain("stats count by dest_ip");
  });
});

// A call that throws has still been paid for. The harness journals the spend
// either way, but the controller reads what a failure cost off the error, and a
// provider error carries no cost field -- so the money landed on the ledger and
// never reached the budget or the report. One run journaled $0.7889 and reported
// $0.1110: nine worker calls died on a dropped upstream stream, every one paid
// for, none counted. A ceiling that cannot see failed work does not hold.
describe("what a failed call cost", () => {
  const harnessSpending = (spent: number) =>
    ({ budget: { spent: { cost_usd: spent } } }) as unknown as Parameters<typeof charged>[0];

  it("attaches the spend to an error that carries none", async () => {
    const failing = Promise.reject(new Error("Error reading stream: connection timed out"));

    const error = await charged(harnessSpending(0.6779), 0, failing).catch((e) => e);

    expect((error as { cost_usd?: number }).cost_usd).toBeCloseTo(0.6779, 6);
  });

  // Only what this call spent, not the pool's running total: the difference is
  // every earlier call in the run, and charging those again would compound.
  it("charges the delta since the call began, not the whole pool", async () => {
    const error = await charged(harnessSpending(1.0), 0.9, Promise.reject(new Error("dead"))).catch((e) => e);

    expect((error as { cost_usd?: number }).cost_usd).toBeCloseTo(0.1, 6);
  });

  // BudgetRefused already carries its own, and it is the authority on the call it
  // refused; overwriting it would report a refusal as though it had been paid for.
  it("leaves a cost the error already states", async () => {
    const stated = Object.assign(new Error("refused"), { cost_usd: 0.02 });

    const error = await charged(harnessSpending(5), 0, Promise.reject(stated)).catch((e) => e);

    expect((error as { cost_usd?: number }).cost_usd).toBe(0.02);
  });

  it("adds nothing to a call that failed before spending", async () => {
    const error = await charged(harnessSpending(0.5), 0.5, Promise.reject(new Error("refused early"))).catch((e) => e);

    expect((error as { cost_usd?: number }).cost_usd).toBeUndefined();
  });

  it("hands a successful call straight back", async () => {
    expect(await charged(harnessSpending(1), 0, Promise.resolve("answered"))).toBe("answered");
  });
});

// One flaky call cost the whole iteration: eight successful searches discarded
// because the write-up call after them died.
describe("what a dispatch keeps when it dies at the write-up", () => {
  it("keeps a successful call's rows, with the query beside them", () => {
    const [record] = salvaged([attempt("splunk_execute", '{"spl_query":"index=botsv3"}', [{ dest_ip: "45.77.53.176" }])]);

    expect(record!.provenance).toBe("unsummarised");
    expect(record!.source_system).toBe("duckdb");
    const gathered = record!.payload["gathered"] as { tool: string; query: string; rows: unknown[] }[];
    expect(gathered).toHaveLength(1);
    expect(gathered[0]!.rows).toEqual([{ dest_ip: "45.77.53.176" }]);
    expect(gathered[0]!.query).toBe('{"spl_query":"index=botsv3"}');
    // Routine, and not a claim anyone made: no role has said what these rows mean.
    expect(record!.salience).toBe("routine");
    expect(record!.attacker_influenceable).toBe(true);
  });

  // Twenty-nine of these reached one board, every one promoted to notable by the
  // attacker_influenceable floor, in a digest that holds twenty-five.
  it("folds a whole dispatch into one record rather than one per call", () => {
    const many = Array.from({ length: 29 }, (_, index) =>
      attempt("splunk_execute", `{"spl_query":"search ${index}"}`, [{ dest_ip: "45.77.53.176" }]),
    );
    const records = salvaged(many);

    expect(records).toHaveLength(1);
    expect(records[0]!.summary).toContain("29 queries");
    expect((records[0]!.payload["gathered"] as unknown[])).toHaveLength(29);
  });

  // Corroboration is counted over source_system, so naming one of several tools
  // would credit it with the others' independence.
  it("refuses to name one source system when several answered", () => {
    const mixed = [
      attempt("splunk_execute", "{}", [{ a: 1 }]),
      { ...attempt("lookup_indicators", "{}", [{ b: 2 }]), result: { ok: true as const, rows: [{ b: 2 }], rowCount: 1, capped: false, sourceSystem: "misp" } },
    ];
    expect(salvaged(mixed as never)[0]!.source_system).toBe("several");
  });

  // The queries are what an analyst re-runs, so they survive the budget even when
  // the rows behind them do not -- and the record says the rows were dropped.
  it("keeps every query but trims rows past the budget, saying so", () => {
    const heavy = Array.from({ length: 12 }, (_, index) =>
      attempt("splunk_execute", `{"spl_query":"search ${index}"}`, [{ blob: "x".repeat(900) }]),
    );
    const gathered = salvaged(heavy)[0]!.payload["gathered"] as { query: string; rows?: unknown[]; rows_dropped?: number }[];

    expect(gathered).toHaveLength(12);
    expect(gathered.every((one) => typeof one.query === "string" && one.query.includes("spl_query"))).toBe(true);
    expect(gathered.some((one) => one.rows_dropped !== undefined)).toBe(true);
  });

  it("keeps nothing from a call that failed or returned nothing", () => {
    const empty = attempt("splunk_execute", "{}", []);
    const failed: Attempt = { ...empty, result: { ok: false, failure: { kind: "timeout", timeoutMs: 30_000 } } };
    expect(salvaged([empty, failed])).toEqual([]);
  });
});

// AbortSignal.any attaches to both sources and lets go only when the composite is
// collected, so one per turn piled a listener per iteration onto the run-long lease
// signal until Node warned about a leak.
describe("the lease signal is not a place listeners accumulate", () => {
  it("lets go of both signals when the turn ends", () => {
    const lease = new AbortController();
    const asked = new AbortController();
    const on = (control: AbortController) => getEventListeners(control.signal, "abort").length;

    for (let turn = 0; turn < 20; turn += 1) {
      const linked = link(lease.signal, asked.signal);
      expect(on(lease)).toBe(1);
      linked.release();
    }

    expect(on(lease)).toBe(0);
    expect(on(asked)).toBe(0);
  });

  // Passing the one signal straight through looked free and was not. The HTTP client
  // attaches an abort listener per request and never removes it, so a role handed the
  // run-long lease signal directly collected one per model call for the whole run --
  // four workers, ten calls each, five iterations, and Node warning it had a leak. A
  // controller per turn costs nothing and dies with the turn, so the accumulation lands
  // on something short-lived.
  it("gives a turn its own signal even when there is only one to honour", () => {
    const lease = new AbortController();
    const linked = link(lease.signal, undefined);

    expect(linked.signal).not.toBe(lease.signal);
    expect(getEventListeners(lease.signal, "abort")).toHaveLength(1);
    linked.release();
    expect(getEventListeners(lease.signal, "abort")).toHaveLength(0);
  });

  // What the leak actually looked like: many calls on one turn's signal. That is
  // bounded and released, so the ceiling is raised rather than the warning silenced.
  it("carries the calls of one turn without warning about them", () => {
    const lease = new AbortController();
    const linked = link(lease.signal, undefined);
    for (let call = 0; call < 30; call += 1) {
      linked.signal!.addEventListener("abort", () => {});
    }

    expect(getEventListeners(linked.signal!, "abort").length).toBeGreaterThan(10);
    expect(getMaxListeners(linked.signal!)).toBeGreaterThan(30);
    // And still only the one relay on the signal the run owns.
    expect(getEventListeners(lease.signal, "abort")).toHaveLength(1);
    linked.release();
  });

  it("relays a single signal's abort to the turn", () => {
    const lease = new AbortController();
    const linked = link(lease.signal, undefined);
    lease.abort(new Error("lease lost"));

    expect(linked.signal!.aborted).toBe(true);
    expect((linked.signal!.reason as Error).message).toBe("lease lost");
  });

  it("still aborts from whichever signal fires", () => {
    for (const firing of ["lease", "asked"] as const) {
      const lease = new AbortController();
      const asked = new AbortController();
      const linked = link(lease.signal, asked.signal);
      (firing === "lease" ? lease : asked).abort(new Error(firing));
      expect(linked.signal!.aborted).toBe(true);
      expect((linked.signal!.reason as Error).message).toBe(firing);
    }
  });

  it("is already aborted when a signal fired before the turn began", () => {
    const lease = new AbortController();
    lease.abort(new Error("lease lost"));
    expect(link(lease.signal, new AbortController().signal).signal!.aborted).toBe(true);
  });
});

// The salvage has to happen on the dispatcher's own failure path, not just in the
// function it calls: returning evidence: [] there is what discarded eight searches.
describe("the dispatcher's failure path, end to end", () => {
  const SEARCH = defineTool(
    {
      id: "splunk_execute",
      description: "run SPL",
      parameters: { type: "object", properties: { spl_query: { type: "string" } } },
      execute: async () => ({ ok: true as const, rows: [{ dest_ip: "45.77.53.176", count: 412 }], rowCount: 1, capped: false, sourceSystem: "cisco:asa" }),
    },
    { maxRows: 10, timeoutMs: 1_000 },
    true,
  );

  const DIED = "read tcp 172.18.0.3:46528->160.79.104.10:443: read: connection timed out";

  const SPEC = {
    arch: "threathunt",
    approvals: [],
    runtime: { max_turns: 4, result_cap: 8_000, recall_limit: 0 },
    roles: { workers: { network_analyst: { prompt: "look", description: "traffic", output_schema: { type: "object" }, tools: [], needs: [] } } },
  } as unknown as RunSpec;

  // One tool call that answers, then the write-up call dies -- the exact shape of
  // every failed dispatch in the run that prompted this.
  function dying(): Harness<HuntKinds> {
    return {
      provider: scriptedProvider([
        { calls: [{ tool: "splunk_execute", args: '{"spl_query":"index=botsv3"}' }], tokens: { input: 900 } },
        { fail: DIED, tokens: { input: 40 } },
      ]),
      registry: registryOf([SEARCH], { network_analyst: ["splunk_execute"] }),
      dispatch: localDispatch,
      budget: budgetOf({ max_calls: 12, max_cost_usd: 5, max_wall_ms: 600_000, max_park_ms: 1_000 }, unmeteredQuota),
      memory: nullMemory,
      state: new InProcessState(),
    } as unknown as Harness<HuntKinds>;
  }

  it("reports the failure and still returns what it gathered", async () => {
    const harness = dying();
    const dispatcher = workerDispatcher({ harness, spec: SPEC, run_id: "run-1", actions: [] });
    const result = await dispatcher.dispatch({
      dispatch_id: "dsp-1",
      agent_id: "network_analyst",
      query_intent: "find beaconing",
      focus: "",
      target_hypothesis_id: null,
      scope: {},
    } as never);

    expect(result.failed).toBe(true);
    // The query ran; only the write-up died. Its rows are the iteration's whole point.
    expect(result.evidence).toHaveLength(1);
    expect(result.evidence[0]!.provenance).toBe("unsummarised");
    const gathered = result.evidence[0]!.payload["gathered"] as { tool: string; rows: unknown[] }[];
    expect(gathered[0]!.tool).toBe("splunk_execute");
    expect(gathered[0]!.rows).toEqual([{ dest_ip: "45.77.53.176", count: 412 }]);
    // Still journaled as an executed call, which is what an analyst re-runs.
    expect(result.calls?.map((call) => call.tool)).toEqual(["splunk_execute"]);
    // The query and the write-up that died, both billed: a ceiling that cannot see
    // failed work is a ceiling a failing run walks straight through. A provider that
    // dies escapes emit()'s retry loop, so the write-up is billed once here -- in a
    // deployment the gateway and the limiter are what retry it.
    expect(harness.budget.spent.tokens.input).toBe(940);
  });
});

// The worker prompt tells it to cite "the hypothesis ids given to you" and to report
// in scope. Both were dropped in the port, so it was told neither.
describe("what a worker is actually told", () => {
  const REQUEST = {
    dispatch_id: "dsp-1",
    hunt_id: "hunt-1",
    agent_id: "network_analyst",
    query_intent: "find hosts beaconing outbound",
    focus: "192.168.3.130 [entity ip:192.168.3.130]",
    target_hypothesis_id: "hyp-7",
    scope: { index: "botsv3", earliest: "2018-08-20", latest: "2018-08-21" },
  };

  it("names the hypothesis, the scope and the scenario", () => {
    const rendered = renderDispatch(REQUEST as never, "Frothly, a brewing company, August 2018.");

    expect(rendered).toContain("find hosts beaconing outbound");
    expect(rendered).toContain("192.168.3.130");
    // Without this the worker cannot set supports/weakens at all.
    expect(rendered).toContain("This bears on hypothesis hyp-7.");
    // Without this it invents its own index and window.
    expect(rendered).toContain('"index":"botsv3"');
    expect(rendered).toContain('"earliest":"2018-08-20"');
    expect(rendered).toContain("Frothly, a brewing company");
  });

  // The lead and the critic both read the run's own brief. A worker told only the
  // playbook's standing one is querying an estate nobody described to it.
  it("carries the run's own brief, not only the playbook's", () => {
    const rendered = renderDispatch(
      REQUEST as never,
      narrativeOf({ narrative: "Standing brief.", prompt: "The data spans 2018-08-19 to 2018-08-20 only." }),
    );
    expect(rendered).toContain("Standing brief.");
    expect(rendered).toContain("2018-08-19 to 2018-08-20");
  });

  it("says nothing about a hypothesis or scope it was not given", () => {
    const bare = renderDispatch({ ...REQUEST, focus: "", target_hypothesis_id: null, scope: {} } as never, "");
    expect(bare).not.toContain("bears on hypothesis");
    expect(bare).not.toContain("## Scope");
    expect(bare).toContain("find hosts beaconing outbound");
  });
});

// The critic argues against the records, so they arrive delimited: raw JSON reads as
// the prompt's own voice, which is what every other evidence path prevents.
describe("what the critic is actually told", () => {
  const CHECK = {
    hypothesis_id: "hyp-7",
    statement: "credentials taken from HOST-42 were reused elsewhere",
    narrative: "Frothly, August 2018.",
    evidence: [
      {
        relation: "supports",
        record: {
          evidence_id: "ev-1",
          source_system: "cisco:asa",
          summary: "412 connections at 300s intervals",
          why_notable: "low jitter",
          payload: { dest_ip: "45.77.53.176" },
          attacker_influenceable: false,
          rests_on: [
            { field: "conn_count", authored: "sensor" },
            { field: "dest_ip", authored: "adversary" },
          ],
        },
      },
    ],
  };

  it("delimits each record and states what the argument turns on", () => {
    const rendered = renderNullCheck(CHECK as never);

    expect(rendered).toContain("[hyp-7] credentials taken from HOST-42 were reused elsewhere");
    // Which half of a record the adversary authored is the argument's best lever, so the
    // critic is told the basis rather than one blanket flag.
    expect(rendered).toContain(
      '<vigil:evidence id="ev-1" relation="supports" source="cisco:asa" sensor_attested="true" rests_on="conn_count:sensor dest_ip:adversary">',
    );
    expect(rendered).toContain("</vigil:evidence>");
    expect(rendered).toContain("45.77.53.176");
    expect(rendered).toContain("Frothly, August 2018.");
  });

  it("says so plainly when nothing is linked, rather than showing an empty list", () => {
    expect(renderNullCheck({ ...CHECK, evidence: [] } as never)).toContain("Nothing is linked to this hypothesis.");
  });
});
