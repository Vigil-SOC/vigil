import { describe, expect, it } from "vitest";
import { archFor } from "../../arch/registry.js";
import type { CheckpointPayload, DispatchPayload, TerminalPayload } from "../../contracts/events.js";
import { budgetOf, unmeteredQuota } from "../../core/budget.js";
import { localDispatch } from "../../core/dispatch.js";
import type { Harness } from "../../core/loop.js";
import { nullMemory } from "../../core/memory.js";
import { registryOf } from "../../core/registry.js";
import { assembleSpec, loadArch, parseConfig, parsePlaybook, type RunSpec } from "../../core/spec.js";
import { InProcessState } from "../../core/state.js";
import { PROVER_TOOL, proveLink, proveOrigin, redact, type Observation, type StepPayload } from "../../workflows/rootcause/proof.js";
import { finishFrom, recordFrom } from "../../workflows/rootcause/tools.js";
import { runRootCause, type RootCauseKinds } from "../../workflows/rootcause/workflow.js";
import { scriptedProvider, type ScriptedTurn } from "../support/scripted-provider.js";
import { ZERO_TOKENS } from "../../contracts/budget.js";
import type { Provider, ProviderEvent, TurnRequest } from "../../core/provider.js";
import { InProcessDirectiveQueue } from "../../workflows/hunt/directives.js";

const RUN = "5a2c2d3e-0000-4000-8000-000000000c38";
const AT = "2024-01-02T03:00:00Z";
const LINK = "invoice.lnk";

const LOCALS = [
  "  - { id: record, kind: local, description: record a step, parameters: { type: object } }",
  "  - { id: finish, kind: local, description: finish the trace, parameters: { type: object } }",
].join("\n");

function specOf(extra = "", checkpoints = ""): RunSpec {
  const entry = archFor("root_cause");
  const text = [
    "model: scripted/model",
    "budgets: { max_calls: 40, max_cost_usd: 15, max_wall_ms: 5400000 }",
    "runtime: { max_turns: 8, result_cap: 8000, recall_limit: 1 }",
    "tools:",
    LOCALS,
    extra,
    checkpoints,
    "approvals: []",
  ]
    .filter((line) => line !== "")
    .join("\n");
  return assembleSpec({
    arch: loadArch(entry.arch, entry.actions),
    playbook: parsePlaybook("name: root-cause\n"),
    config: parseConfig(text, entry.owned),
    prompt: "FYODOR-L is beaconing to 203.0.113.5",
  });
}

const SPLUNK = `  - { id: ${PROVER_TOOL}, kind: remote, provides: telemetry_search, description: run SPL, parameters: { type: object } }`;
const ELASTIC = "  - { id: elastic_search_logs, kind: remote, provides: telemetry_search, description: search logs, parameters: { type: object } }";

function harnessOf(
  script: readonly ScriptedTurn[],
  spec: RunSpec,
  state = new InProcessState<RootCauseKinds>(),
  budget = budgetOf(spec.budgets, unmeteredQuota),
): Harness<RootCauseKinds> & { provider: ReturnType<typeof scriptedProvider> } {
  const provider = scriptedProvider(script);
  return {
    provider,
    registry: registryOf([], {}),
    dispatch: localDispatch,
    budget,
    memory: nullMemory,
    state,
  };
}

function observation(tool: string, args: string, rows: readonly unknown[]): Observation {
  return { tool, args, rows };
}

function count(tool: string, value: string, at: string, n: number): Observation {
  return observation(tool, `search ${value} before ${at}`, [{ count: n }]);
}

function hit(value: string): Observation {
  return observation(PROVER_TOOL, "", [{ file: value }]);
}

const BOTH = [hit(LINK), hit(LINK)];
const ZERO = count(PROVER_TOOL, LINK, AT, 0);

describe("a link is a value both events carry", () => {
  it("is proved only by two results and a zero count before the cause", () => {
    expect(proveLink(LINK, [], AT, [...BOTH, ZERO])).toBe("proven");
  });

  it("rejects an IP, a timestamp, or the step's who", () => {
    expect(proveLink("203.0.113.5", [], AT, [...BOTH, ZERO])).toBe("rejected");
    expect(proveLink("2001:db8::1", [], AT, [...BOTH, ZERO])).toBe("rejected");
    expect(proveLink(AT, [], AT, [...BOTH, ZERO])).toBe("rejected");
    expect(proveLink("10:01:39", [], AT, [...BOTH, ZERO])).toBe("rejected");
    expect(proveLink("alice", ["alice"], AT, [...BOTH, ZERO])).toBe("rejected");
  });

  it("rejects a value counted before the cause and leaves a missing count unproven", () => {
    expect(proveLink(LINK, [], AT, [...BOTH, count(PROVER_TOOL, LINK, AT, 4)])).toBe("rejected");
    expect(proveLink(LINK, [], AT, BOTH)).toBe("unproven");
  });

  it("treats an empty hit list as nothing", () => {
    const empty = observation(PROVER_TOOL, `search ${LINK} before ${AT}`, []);
    expect(proveLink(LINK, [], AT, [empty, empty, ZERO])).toBe("unproven");
    expect(proveLink(LINK, [], AT, [hit(LINK), empty, ZERO])).toBe("unproven");
  });

  // Splunk refuses an ISO time in latest=; the count the model can actually run
  // names the cause time in epoch seconds.
  it("counts before the cause time when the search names it in epoch seconds", () => {
    const epoch = observation(PROVER_TOOL, `index=main "${LINK}" latest=1704164400 | stats count`, [{ count: 0 }]);
    expect(proveLink(LINK, [], AT, [...BOTH, epoch])).toBe("proven");
    expect(proveOrigin(LINK, [], AT, [epoch])).toBe("proven");
  });

  it("does not take another moment's epoch, or the digits inside a longer number", () => {
    const hourLater = observation(PROVER_TOOL, `"${LINK}" latest=1704168000 | stats count`, [{ count: 0 }]);
    const longer = observation(PROVER_TOOL, `"${LINK}" latest=17041644001 | stats count`, [{ count: 0 }]);
    expect(proveOrigin(LINK, [], AT, [hourLater])).toBe("unproven");
    expect(proveOrigin(LINK, [], AT, [longer])).toBe("unproven");
  });

  // What invoke journals for splunk_execute: the tool's envelope, with the
  // `| stats count` row inside `results`.
  function wrapped(args: string, inner: readonly unknown[]): Observation {
    return observation(PROVER_TOOL, args, [{ success: true, query: args, count: inner.length, results: inner }]);
  }

  it("reads the count inside the splunk envelope, not the envelope's row count", () => {
    const zero = wrapped(`index=main "${LINK}" latest=1704164400 | stats count`, [{ count: "0" }]);
    expect(proveLink(LINK, [], AT, [...BOTH, zero])).toBe("proven");
    expect(proveLink(LINK, [], AT, [hit(LINK), zero])).toBe("unproven");
    expect(proveOrigin(LINK, [], AT, [zero])).toBe("proven");
  });

  it("rejects a count above zero inside the envelope", () => {
    const seen = wrapped(`index=main "${LINK}" latest=1704164400 | stats count`, [{ count: "58" }]);
    expect(proveLink(LINK, [], AT, [...BOTH, seen])).toBe("rejected");
    expect(proveOrigin(LINK, [], AT, [seen])).toBe("rejected");
  });

  it("leaves an envelope with no rows, another second, or a longer number unproven", () => {
    const none = wrapped(`index=main "${LINK}" latest=1704164400`, []);
    const hourLater = wrapped(`"${LINK}" latest=1704168000 | stats count`, [{ count: "0" }]);
    const longer = wrapped(`"${LINK}" latest=17041644001 | stats count`, [{ count: "0" }]);
    expect(proveOrigin(LINK, [], AT, [none])).toBe("unproven");
    expect(proveOrigin(LINK, [], AT, [hourLater])).toBe("unproven");
    expect(proveOrigin(LINK, [], AT, [longer])).toBe("unproven");
  });

  it("does not let any other telemetry tool prove the count", () => {
    const other = count("splunk_execute", LINK, AT, 0);
    const elastic = count("elastic_search_logs", LINK, AT, 0);
    expect(proveLink(LINK, [], AT, [...BOTH, other])).toBe("unproven");
    expect(proveLink(LINK, [], AT, [...BOTH, elastic])).toBe("unproven");
  });
});

describe("an origin is the same count, one step further", () => {
  it("is proved by a zero count of the link, or of the starting artifact", () => {
    expect(proveOrigin(LINK, [], AT, [ZERO])).toBe("proven");
    expect(proveOrigin("payload.exe", [], AT, [count(PROVER_TOOL, "payload.exe", AT, 0)])).toBe("proven");
  });

  it("is rejected when earlier events carry the value", () => {
    expect(proveOrigin(LINK, [], AT, [count(PROVER_TOOL, LINK, AT, 2)])).toBe("rejected");
  });

  it("is not proved by a count of the actor", () => {
    expect(proveOrigin("alice", ["alice"], AT, [count(PROVER_TOOL, "alice", AT, 0)])).toBe("rejected");
  });
});

describe("a who that is not on a proven step", () => {
  const proven: StepPayload = {
    step_id: "step-1",
    event: "file landed",
    who: "alice",
    at: AT,
    link: LINK,
    artifact: "",
    cause_id: null,
    origin: true,
    link_status: "none",
    origin_status: "proven",
  };
  const open: StepPayload = { ...proven, step_id: "step-2", who: "mallory", origin: false, origin_status: "none" };

  it("is replaced with [unlinked]", () => {
    expect(redact("alice delivered it; mallory was nearby", [proven, open])).toBe(
      "alice delivered it; [unlinked] was nearby",
    );
  });
});

describe("finish", () => {
  const limits = specOf(SPLUNK).budgets;

  it("is an error while a cause is open and the ceilings have not been hit", async () => {
    const state = new InProcessState<RootCauseKinds>();
    const result = await finishFrom(state, RUN, limits)({}, { maxRows: 1, timeoutMs: 1 }, new AbortController().signal);
    expect(result.ok).toBe(false);
    if (!result.ok && result.failure.kind === "refused") expect(result.failure.detail).toMatch(/nothing has been recorded/);
  });

  it("succeeds at the wall ceiling and names the open step", async () => {
    const state = new InProcessState<RootCauseKinds>();
    await state.append(RUN, [
      {
        run_id: RUN,
        run_kind: "root_cause",
        kind: "run",
        payload: {
          run_kind: "root_cause",
          spec: {},
          budgets: limits,
          seed: RUN,
          tenant_id: null,
          started_by: "test",
        },
      },
    ]);
    const later = Date.now() + limits.max_wall_ms + 1_000;
    const result = await finishFrom(state, RUN, limits, () => later)({}, { maxRows: 1, timeoutMs: 1 }, new AbortController().signal);
    expect(result.ok).toBe(true);
  });
});

describe("record wires the proof to the cause step", () => {
  it("proves the link against the cause time, not the later event", async () => {
    const state = new InProcessState<RootCauseKinds>();
    const causeAt = "2024-01-02T01:00:00Z";
    await state.append(RUN, [
      dispatch("d1", PROVER_TOOL, `search ${LINK} before ${causeAt}`, [{ count: 0 }]),
      dispatch("d2", PROVER_TOOL, "", [{ file: LINK, at: causeAt }]),
      dispatch("d3", PROVER_TOOL, "", [{ file: LINK, at: AT }]),
    ]);
    const bounds = { maxRows: 5, timeoutMs: 1_000 };
    const signal = new AbortController().signal;
    const origin = await recordFrom(state, RUN)(
      { event: "file written", who: "alice", at: causeAt, link: LINK, origin: true },
      bounds,
      signal,
    );
    expect(origin.ok).toBe(true);
    const later = await recordFrom(state, RUN)(
      { event: "process started", who: "svc", at: AT, link: LINK, cause_id: "step-1" },
      bounds,
      signal,
    );
    expect(later.ok).toBe(true);
    if (later.ok) expect(later.rows[0]).toMatchObject({ link_status: "proven", proven: true });
  });
});

describe("the trace", () => {
  it("proves a step from journaled splunk results and redacts an unlinked who", async () => {
    const spec = specOf(SPLUNK);
    const state = new InProcessState<RootCauseKinds>();
    await state.append(RUN, [
      runEvent(spec),
      dispatch("d1", PROVER_TOOL, `search ${LINK} before ${AT}`, [{ count: 0 }]),
    ]);
    const harness = harnessOf(
      [
        {
          calls: [
            {
              tool: "record",
              args: JSON.stringify({ event: "file landed", who: "alice", at: AT, link: LINK, origin: true, artifact: LINK }),
            },
          ],
        },
        { calls: [{ tool: "record", args: JSON.stringify({ event: "name on the alert", who: "mallory", at: AT }) }] },
        { calls: [{ tool: "finish", args: "{}" }] },
        { content: "alice delivered invoice.lnk; mallory was nearby" },
      ],
      spec,
      state,
    );

    const report = await runRootCause(harness, { run_id: RUN, spec });
    expect(report.status).toBe("completed");
    const terminal = (await state.read(RUN)).find((event) => event.kind === "terminal");
    const summary = (terminal?.payload as TerminalPayload).summary ?? "";
    expect(summary).toContain("alice delivered invoice.lnk; [unlinked] was nearby");
    expect(summary).toContain("Still unproven: step-2");
    const steps = (await state.read(RUN)).filter((event) => event.kind === "step").map((event) => event.payload as StepPayload);
    expect(steps[0]?.origin_status).toBe("proven");
    const seen = harness.provider.requests.map((request) => JSON.stringify(request)).join("\n");
    expect(seen).toContain("finish refused");
    expect(seen).toContain("step-1");
  });

  it("names the open steps when the model stops in prose before they are proven", async () => {
    const spec = specOf(SPLUNK);
    const state = new InProcessState<RootCauseKinds>();
    const harness = harnessOf(
      [
        { calls: [{ tool: "record", args: JSON.stringify({ event: "phishing mail opened", who: "bob", at: AT, link: LINK, origin: true }) }] },
        { content: "The attacker came in through phishing." },
      ],
      spec,
      state,
    );
    const report = await runRootCause(harness, { run_id: RUN, spec });
    expect(report.status).toBe("completed");
    expect(report.reason).toMatch(/still open/);
    const terminal = (await state.read(RUN)).find((event) => event.kind === "terminal");
    const summary = (terminal?.payload as TerminalPayload).summary ?? "";
    expect(summary).toContain("The attacker came in through phishing.");
    expect(summary).toContain("Still unproven: step-1 (phishing mail opened)");
    expect(summary).not.toContain("bob");
  });

  it("says nothing was recorded when the model stops in prose without a step", async () => {
    const spec = specOf(SPLUNK);
    const state = new InProcessState<RootCauseKinds>();
    const report = await runRootCause(harnessOf([{ content: "Nothing to trace." }], spec, state), { run_id: RUN, spec });
    expect(report.reason).toMatch(/still open/);
    const terminal = (await state.read(RUN)).find((event) => event.kind === "terminal");
    expect((terminal?.payload as TerminalPayload).summary).toContain("Still unproven: nothing was recorded.");
  });

  it("reports a prose stop as finished when every step is proven", async () => {
    const spec = specOf(SPLUNK);
    const state = new InProcessState<RootCauseKinds>();
    await state.append(RUN, [runEvent(spec), dispatch("d1", PROVER_TOOL, `search ${LINK} before ${AT}`, [{ count: 0 }])]);
    const harness = harnessOf(
      [
        { calls: [{ tool: "record", args: JSON.stringify({ event: "file landed", who: "alice", at: AT, link: LINK, origin: true, artifact: LINK }) }] },
        { content: "alice delivered invoice.lnk" },
      ],
      spec,
      state,
    );
    const report = await runRootCause(harness, { run_id: RUN, spec });
    expect(report.reason).toBe("the trace finished");
    const terminal = (await state.read(RUN)).find((event) => event.kind === "terminal");
    expect((terminal?.payload as TerminalPayload).summary).toBe("alice delivered invoice.lnk");
  });

  it("journals a remote result and does not prove from a paraphrase", async () => {
    const spec = specOf(SPLUNK);
    const state = new InProcessState<RootCauseKinds>();
    const harness = harnessOf(
      [{ calls: [{ tool: PROVER_TOOL, args: "{\"spl\":\"search\"}" }] }, { content: "done" }],
      spec,
      state,
    );
    await runRootCause(harness, { run_id: RUN, spec });
    const dispatches = (await state.read(RUN)).filter((event) => event.kind === "dispatch");
    expect(dispatches).toHaveLength(1);
    const call = ((dispatches[0]!.payload as DispatchPayload).calls as { tool: string }[])[0];
    expect(call?.tool).toBe(PROVER_TOOL);
  });

  it("completes at the cost ceiling and names the open step", async () => {
    const spec = specOf(SPLUNK);
    const state = new InProcessState<RootCauseKinds>();
    await state.append(RUN, [
      runEvent(spec),
      {
        run_id: RUN,
        run_kind: "root_cause",
        kind: "step",
        payload: {
          step_id: "step-1",
          event: "alice ran the beacon",
          who: "alice",
          at: AT,
          link: "",
          artifact: "",
          cause_id: null,
          origin: false,
          link_status: "none",
          origin_status: "none",
        } satisfies StepPayload,
      },
    ]);
    const harness = harnessOf([], spec, state, budgetOf(spec.budgets, { spent: async () => ({ used_usd: 20, limit_usd: 15 }) }));
    const report = await runRootCause(harness, { run_id: RUN, spec });
    expect(report.status).toBe("completed");
    expect(report.reason).toMatch(/ceiling/);
    const terminal = (await state.read(RUN)).find((event) => event.kind === "terminal");
    const summary = (terminal?.payload as TerminalPayload).summary ?? "";
    expect(summary).toContain("step-1");
    expect(summary).toContain("[unlinked]");
    expect(summary).not.toContain("alice");
    expect(harness.provider.requests).toHaveLength(0);
  });

  it("completes before a model call when telemetry is unbound", async () => {
    const spec = specOf();
    const harness = harnessOf([], spec);
    const report = await runRootCause(harness, { run_id: RUN, spec });
    expect(report.status).toBe("completed");
    expect(report.reason).toMatch(/No telemetry_search/);
    expect(harness.provider.requests).toHaveLength(0);
  });

  it("says once that a non-splunk source cannot prove a link", async () => {
    const spec = specOf(ELASTIC);
    const state = new InProcessState<RootCauseKinds>();
    const harness = harnessOf([{ content: "I looked" }], spec, state);
    const report = await runRootCause(harness, { run_id: RUN, spec });
    expect(report.status).toBe("completed");
    const notices = (await state.read(RUN)).filter((event) => event.kind === "notice");
    expect(notices).toHaveLength(1);
    expect((await state.read(RUN)).find((event) => event.kind === "terminal")?.payload).toMatchObject({
      summary: expect.stringContaining("cannot be proved"),
    });
    const again = harnessOf([{ content: "again" }], spec, state);
    await runRootCause(again, { run_id: RUN, spec });
    expect((await state.read(RUN)).filter((event) => event.kind === "notice")).toHaveLength(1);
    expect(again.provider.requests).toHaveLength(0);
  });

  it("parks before any model call, then permits or rejects", async () => {
    const spec = specOf(SPLUNK, "checkpoints:\n  hypothesis_approval: ask");
    const state = new InProcessState<RootCauseKinds>();
    const announced: string[] = [];
    const parked = await runRootCause(harnessOf([], spec, state), {
      run_id: RUN,
      spec,
      announce: async (_run, _kind, payload) => {
        announced.push(payload.question);
      },
    });
    expect(parked.status).toBe("waiting_approval");
    expect(announced[0]).toContain("Permit a root-cause trace of this finding?");
    expect(announced[0]).toContain("FYODOR-L is beaconing");
    const checkpoint = (await state.read(RUN)).find((event) => event.kind === "checkpoint");
    expect((checkpoint?.payload as CheckpointPayload).checkpoint_class).toBe("hypothesis_approval");

    const rejected = new InProcessState<RootCauseKinds>();
    await runRootCause(harnessOf([], spec, rejected), { run_id: RUN, spec });
    const refusal = await runRootCause(harnessOf([], spec, rejected), {
      run_id: RUN,
      spec,
      answers: async () => [
        { checkpoint_id: "cp-root-cause-permit", actor: "sam", answer: "reject", text: "not this finding", resolved_at: AT },
      ],
    });
    expect(refusal.status).toBe("failed");
    expect(refusal.reason).toBe("not this finding");

    const permitted = harnessOf([{ content: "tracing" }], spec, state);
    const report = await runRootCause(permitted, {
      run_id: RUN,
      spec,
      answers: async () => [
        { checkpoint_id: "cp-root-cause-permit", actor: "sam", answer: "approve", text: "go", resolved_at: AT },
      ],
    });
    expect(report.status).toBe("completed");
    expect(permitted.provider.requests).toHaveLength(1);
  });
});

describe("an operator's stop", () => {
  const ABORT = { directive_id: "dir-abort", actor: "sam", kind: "abort" as const, text: "enough evidence", created_at: AT };

  // A model call that never answers: it ends only when its signal is aborted.
  const hanging: Provider = {
    model: "scripted/model",
    provider_type: "scripted",
    stream: async function* (request: TurnRequest): AsyncGenerator<ProviderEvent> {
      await new Promise((_, reject) => {
        const signal = request.signal;
        if (signal === undefined) return;
        if (signal.aborted) reject(signal.reason);
        signal.addEventListener("abort", () => reject(signal.reason), { once: true });
      });
      yield { type: "usage", tokens: ZERO_TOKENS };
    },
  };

  async function terminalsOf(state: InProcessState<RootCauseKinds>): Promise<TerminalPayload[]> {
    return (await state.read(RUN)).filter((event) => event.kind === "terminal").map((event) => event.payload as TerminalPayload);
  }

  it("ends the run aborted before a model call when a stop is already queued", async () => {
    const spec = specOf(SPLUNK);
    const state = new InProcessState<RootCauseKinds>();
    const queue = new InProcessDirectiveQueue();
    await queue.enqueue(RUN, ABORT);
    const harness = harnessOf([], spec, state);
    const report = await runRootCause(harness, { run_id: RUN, spec, queue });
    expect(report.status).toBe("aborted");
    expect(report.reason).toContain("enough evidence");
    expect(harness.provider.requests).toHaveLength(0);
    expect(await terminalsOf(state)).toHaveLength(1);
    expect((await state.read(RUN)).some((event) => event.kind === "directive")).toBe(true);
  });

  it("stops a model call in flight within a poll of the stop being queued", async () => {
    const spec = specOf(SPLUNK);
    const state = new InProcessState<RootCauseKinds>();
    const queue = new InProcessDirectiveQueue();
    const harness = { ...harnessOf([], spec, state), provider: hanging };
    const started = Date.now();
    const running = runRootCause(harness, { run_id: RUN, spec, queue });
    setTimeout(() => void queue.enqueue(RUN, ABORT), 50);
    const report = await running;
    expect(report.status).toBe("aborted");
    expect(Date.now() - started).toBeLessThan(2_000);
    const terminals = await terminalsOf(state);
    expect(terminals).toHaveLength(1);
    expect(terminals[0]?.outcome).toBe("aborted");
  }, 5_000);

  it("does not journal a second terminal when the ledger was ended under it", async () => {
    const spec = specOf(SPLUNK);
    const state = new InProcessState<RootCauseKinds>();
    const backstop: Provider = {
      model: "scripted/model",
      provider_type: "scripted",
      stream: async function* (): AsyncGenerator<ProviderEvent> {
        await state.append(RUN, [
          { run_id: RUN, run_kind: "root_cause", kind: "terminal", payload: { outcome: "aborted", reason: "stopped (did not stop on request)" } },
        ]);
        yield { type: "text_delta", text: "still tracing" };
        yield { type: "usage", tokens: ZERO_TOKENS };
      },
    };
    const report = await runRootCause({ ...harnessOf([], spec, state), provider: backstop }, { run_id: RUN, spec });
    expect(report.status).toBe("aborted");
    expect(await terminalsOf(state)).toHaveLength(1);
  });
});

function runEvent(spec: RunSpec) {
  return {
    run_id: RUN,
    run_kind: "root_cause" as const,
    kind: "run" as const,
    payload: { run_kind: "root_cause" as const, spec, budgets: spec.budgets, seed: RUN, tenant_id: null, started_by: "test" },
  };
}

function dispatch(id: string, tool: string, args: string, rows: unknown[]) {
  return {
    run_id: RUN,
    run_kind: "root_cause" as const,
    kind: "dispatch" as const,
    payload: {
      dispatch_id: id,
      agent_id: "lead",
      status: "complete" as const,
      question_id: null,
      failure_reason: null,
      result: { ok: true as const, rows, rowCount: rows.length, capped: false, sourceSystem: "splunk" },
      calls: [{ tool, arguments: args }],
    },
  };
}
