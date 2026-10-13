import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { archFor } from "../../arch/registry.js";
import { ZERO_TOKENS } from "../../contracts/budget.js";
import type { TerminalHandoff } from "../../contracts/events.js";
import type { ToolResult } from "../../contracts/tool.js";
import { budgetOf, unmeteredQuota } from "../../core/budget.js";
import type { Harness } from "../../core/loop.js";
import { nullMemory } from "../../core/memory.js";
import type { Message, Provider, ProviderEvent, ToolCall, TurnRequest } from "../../core/provider.js";
import { registryOf } from "../../core/registry.js";
import type { ToolDispatch } from "../../core/seams.js";
import { buildSpec, type RunSpec } from "../../core/spec.js";
import { InProcessState } from "../../core/state.js";
import { InProcessDirectiveQueue } from "../../workflows/hunt/directives.js";
import { huntDistil } from "../../workflows/hunt/distil.js";
import type { HuntEvent, HuntKinds } from "../../workflows/hunt/ledger.js";
import { fold } from "../../workflows/hunt/ledger.js";
import { huntProjection } from "../../workflows/hunt/projection.js";
import { runHunt } from "../../workflows/hunt/workflow.js";
import { huntKernelReplay, huntView } from "../../plugins/hunt/view.js";

const FIXTURES = join(import.meta.dirname, "..", "fixtures");
const RUN = "7d3c2d3e-0000-4000-8000-00000000b001";
const BEACON = "10.0.0.5 is beaconing to 45.77.53.176";

function specOf(kind: "hunt" | "adjudicate" = "hunt", hypotheses = [BEACON]): RunSpec {
  const entry = archFor(kind);
  const spec = buildSpec({ arch: entry.arch, playbook: join(FIXTURES, "hunt.playbook.yaml"), config: join(FIXTURES, "hunt.config.yaml") }, entry.actions, entry.owned);
  return { ...spec, sections: { ...spec.sections, hypotheses } };
}

// What the model would see: the board in its brief and every tool result since.
function shown(request: TurnRequest): string {
  return request.messages.map((message: Message) => message.content).join("\n");
}

function hypothesisIn(request: TurnRequest, status = "active"): string {
  const found = new RegExp(`- (h-[0-9a-f]+) \\[${status}\\]`).exec(shown(request));
  if (found === null) throw new Error("no hypothesis on the board");
  return found[1] as string;
}


type Step = (request: TurnRequest) => { tool: string; args: unknown } | { say: string } | { emit: unknown };

// A model that reads the ids off the board and results, as a real one does. Asked for an
// emission (the critic's argument, the narrator's account) it answers by schema.
function modelOf(steps: readonly Step[], answers: { critic?: unknown } = {}): Provider & { requests: TurnRequest[] } {
  const requests: TurnRequest[] = [];
  let at = 0;
  return {
    model: "scripted/model",
    provider_type: "scripted",
    requests,
    stream: async function* (request: TurnRequest): AsyncGenerator<ProviderEvent> {
      requests.push(request);
      if (request.emit !== undefined) {
        const properties = (request.emit["properties"] ?? {}) as Record<string, unknown>;
        const answer = "benign_explanation_stands" in properties
          ? (answers.critic ?? { benign_explanation: "a backup job", benign_explanation_stands: false, rationale: "the interval is too regular for a backup" })
          : { summary: "the host beaconed", what_happened: "regular outbound flows", next_steps: ["isolate 10.0.0.5"] };
        yield { type: "text_delta", text: JSON.stringify(answer) };
        yield { type: "usage", tokens: ZERO_TOKENS };
        return;
      }
      // Offered nothing to call -- the critic's or the narrator's turn before it is
      // asked for its answer -- it has nothing to do but stop.
      if (request.tools.length === 0) {
        yield { type: "usage", tokens: ZERO_TOKENS };
        return;
      }
      const step = steps[at];
      at += 1;
      if (step === undefined) {
        yield { type: "usage", tokens: ZERO_TOKENS };
        return;
      }
      const next = step(request);
      if ("say" in next) {
        yield { type: "text_delta", text: next.say };
        yield { type: "usage", tokens: ZERO_TOKENS };
        return;
      }
      if ("emit" in next) throw new Error("this model emits only when asked");
      yield { type: "usage", tokens: ZERO_TOKENS };
      const call: ToolCall = { id: `c${at}`, tool: next.tool, args: JSON.stringify(next.args) };
      yield { type: "tool_call", call };
    },
  };
}

// The estate: telemetry answers with rows. A local tool -- the hunt's own -- runs here.
function estate(failing: ReadonlySet<string> = new Set()): ToolDispatch {
  return {
    invoke: async (tool, args): Promise<ToolResult> => {
      if (tool.local) return tool.invoke(args);
      if (failing.has(tool.id)) return { ok: false, failure: { kind: "unavailable", detail: `${tool.id} is down` } };
      return { ok: true, rows: [{ src: "10.0.0.5", dst: "45.77.53.176", interval_s: 300, connections: 412 }], rowCount: 1, capped: false, sourceSystem: tool.id };
    },
  };
}

function harnessOf(spec: RunSpec, provider: Provider, state: InProcessState<HuntKinds>, dispatch: ToolDispatch = estate()): Harness<HuntKinds> {
  return { provider, registry: registryOf([], {}), dispatch, budget: budgetOf({ ...spec.budgets, max_calls: 40 }, unmeteredQuota), memory: nullMemory, state };
}

const finding = (source: string, hypothesis: string) => ({
  source_system: source,
  summary: `every 300s +/- 4s over 6 hours, 412 connections from 10.0.0.5 to 45.77.53.176 (${source})`,
  salience: "anomalous",
  why_notable: "the jitter is too low for a person or a poller",
  payload: JSON.stringify({ interval_s: 300, connections: 412 }),
  supports: [hypothesis],
  rests_on: [{ field: "interval_s", authored: "sensor" }],
});

describe("a hunt run by one agent", () => {
  it("queries, records, wins a verdict, escalates and concludes on the hunt's own record", async () => {
    const state = new InProcessState<HuntKinds>();
    const handed: TerminalHandoff[] = [];
    const steps: Step[] = [
      () => ({ tool: "load_skill", args: { name: "hunt-method" } }),
      () => ({ tool: "splunk_search", args: { sql: "select * from flows where src = '10.0.0.5'" } }),
      () => ({ tool: "search_findings", args: { query: "45.77.53.176" } }),
      (request) => ({ tool: "record_evidence", args: { results: [finding("network", hypothesisIn(request)), finding("endpoint", hypothesisIn(request))] } }),
      (request) => ({ tool: "validate_hypothesis", args: { hypothesis_id: hypothesisIn(request) } }),
      (request) => ({ tool: "handoff_ir", args: { hypothesis_id: hypothesisIn(request), rationale: "isolate 10.0.0.5 first" } }),
      () => ({ tool: "conclude_hunt", args: { reason: "the beacon is proven and handed off" } }),
    ];
    const provider = modelOf(steps);

    const report = await runHunt(harnessOf(specOf(), provider, state), {
      run_id: RUN,
      run_kind: "hunt",
      spec: specOf(),
      actions: archFor("hunt").actions,
      queue: new InProcessDirectiveQueue(),
      onHandoff: async (_run, handoff) => {
        handed.push(handoff);
        return true;
      },
    });

    expect(report.status).toBe("completed");
    const events = (await state.read(RUN)) as readonly HuntEvent[];
    const kinds = new Set(events.map((event) => String(event.kind)));
    for (const kind of ["evidence", "link", "handoff", "finalize", "narrative", "call", "terminal"]) expect(kinds.has(kind)).toBe(true);

    const projection = fold(events);
    const [hypothesis] = [...projection.hypotheses.values()];
    expect(hypothesis?.status).toBe("handed_off");
    expect(projection.hunt.outcome).toBe("completed");
    // The critic argued the null against the raw payloads, and lost.
    expect([...projection.evidence.values()].some((record) => record.provenance === "null_check")).toBe(true);

    // Escalated the moment it landed, and carried out on the terminal too.
    expect(handed).toHaveLength(1);
    const terminal = await state.terminal(RUN);
    expect(terminal?.handoffs).toHaveLength(1);
    expect(terminal?.summary).toContain("#");

    // What episodic memory reads, folded off a kernel ledger exactly as off a controller one.
    const distil = huntDistil(RUN, events);
    const [conclusion] = distil.conclusions;
    expect(conclusion).toMatchObject({ status: "handed_off", evidence_count: 2 });
    expect(conclusion?.sources.map((source) => source.source_system).sort()).toEqual(["endpoint", "network"]);
    expect(distil.sightings.some((sighting) => sighting.entity.value === "45.77.53.176")).toBe(true);
    expect(distil.concluded_at).not.toBe("");

    // And what the console reads: the hunt's own projection, its moves read off the calls.
    expect(huntProjection(RUN, events).hypotheses[0]).toMatchObject({ status: "handed_off" });
    const view = huntView(RUN, events);
    expect(view.moves.map((move) => move.action)).toEqual(["CONCLUDE", "HANDOFF_IR", "VALIDATE", "INVESTIGATE", "INVESTIGATE", "INVESTIGATE"]);
    expect(huntKernelReplay(RUN, events).turns.length).toBeGreaterThan(0);
  });

  it("refuses to conclude while a hypothesis could still be moved, and the agent carries on", async () => {
    const state = new InProcessState<HuntKinds>();
    const provider = modelOf([() => ({ tool: "conclude_hunt", args: { reason: "looks fine" } }), () => ({ say: "I will stop here." })]);
    await runHunt(harnessOf(specOf(), provider, state), { run_id: RUN, run_kind: "hunt", spec: specOf(), actions: [], queue: new InProcessDirectiveQueue() });

    const refused = (await state.read(RUN)).find((event) => event.kind === "call" && (event.payload as { tool: string }).tool === "conclude_hunt");
    expect((refused?.payload as { ok: boolean; result: string }).ok).toBe(false);
    expect((refused?.payload as { result: string }).result).toContain("refused");
    // Stopping without a verdict closes the hypothesis inconclusive, never disproven.
    const projection = fold((await state.read(RUN)) as readonly HuntEvent[]);
    expect([...projection.hypotheses.values()][0]?.status).toBe("inconclusive");
  });

  it("records a search the estate could not answer as a blind spot", async () => {
    const state = new InProcessState<HuntKinds>();
    const provider = modelOf([() => ({ tool: "splunk_search", args: { sql: "select 1" } }), () => ({ say: "the SIEM is down" })]);
    await runHunt(harnessOf(specOf(), provider, state, estate(new Set(["splunk_search"]))), {
      run_id: RUN,
      run_kind: "hunt",
      spec: specOf(),
      actions: [],
      queue: new InProcessDirectiveQueue(),
    });

    const projection = fold((await state.read(RUN)) as readonly HuntEvent[]);
    const gaps = [...projection.evidence.values()].filter((record) => record.provenance === "tool_failure");
    expect(gaps).toHaveLength(1);
    expect(gaps[0]?.payload).toMatchObject({ tool: "splunk_search", kind: "unavailable" });
  });

  it("will not prove a hypothesis on one source system, however sure the agent is", async () => {
    const state = new InProcessState<HuntKinds>();
    const provider = modelOf([
      (request) => ({ tool: "record_evidence", args: { results: [finding("network", hypothesisIn(request))] } }),
      (request) => ({ tool: "validate_hypothesis", args: { hypothesis_id: hypothesisIn(request) } }),
      () => ({ say: "done" }),
    ]);
    await runHunt(harnessOf(specOf(), provider, state), { run_id: RUN, run_kind: "hunt", spec: specOf(), actions: [], queue: new InProcessDirectiveQueue() });

    const validated = (await state.read(RUN)).find((event) => event.kind === "call" && (event.payload as { tool: string }).tool === "validate_hypothesis");
    expect((validated?.payload as { result: string }).result).toContain("stays active");
    const projection = fold((await state.read(RUN)) as readonly HuntEvent[]);
    expect([...projection.hypotheses.values()][0]?.status).not.toBe("proven");
  });
});

describe("what a kernel hunt tells the console as it goes", () => {
  it("reports its iteration and ceiling after every call, for the case list (#2209)", async () => {
    const state = new InProcessState<HuntKinds>();
    const progress: [string, number, number][] = [];
    const provider = modelOf([
      () => ({ tool: "splunk_search", args: { sql: "select 1" } }),
      () => ({ tool: "search_findings", args: { query: "45.77.53.176" } }),
      () => ({ say: "done" }),
    ]);
    await runHunt(harnessOf(specOf(), provider, state), {
      run_id: RUN,
      run_kind: "hunt",
      spec: specOf(),
      actions: [],
      queue: new InProcessDirectiveQueue(),
      onProgress: async (runId, iteration, maxCostUsd) => void progress.push([runId, iteration, maxCostUsd]),
    });

    const ceiling = fold((await state.read(RUN)) as readonly HuntEvent[]).hunt.budgets.max_cost_usd;
    expect(progress.map(([, iteration]) => iteration)).toEqual([1, 2]);
    expect(progress.every(([runId, , cap]) => runId === RUN && cap === ceiling)).toBe(true);
  });

  it("keeps going when the progress push fails", async () => {
    const state = new InProcessState<HuntKinds>();
    const provider = modelOf([() => ({ tool: "splunk_search", args: { sql: "select 1" } }), () => ({ say: "done" })]);
    const report = await runHunt(harnessOf(specOf(), provider, state), {
      run_id: RUN,
      run_kind: "hunt",
      spec: specOf(),
      actions: [],
      queue: new InProcessDirectiveQueue(),
      onProgress: async () => {
        throw new Error("the backend is down");
      },
    });
    expect(report.status).toBe("completed");
  });
});

describe("a failed query the agent records (#2207)", () => {
  it("is filed as a blind spot, never linked, and a ruling on it is refused", async () => {
    const state = new InProcessState<HuntKinds>();
    const timedOut = (hypothesis: string) => ({
      source_system: "okta",
      summary: "Raw Okta records for 185.220.101.47 were not retrieved: the query timed out",
      salience: "routine",
      why_notable: "nothing came back",
      payload: "{}",
      weakens: [hypothesis],
    });
    let gapId = "";
    const provider = modelOf([
      (request) => ({ tool: "record_evidence", args: { results: [timedOut(hypothesisIn(request))] } }),
      (request) => {
        gapId = /"evidence_ids":\s*\[\s*"(ev-[^"]+)"/.exec(shown(request))?.[1] ?? "";
        return { tool: "rule_evidence", args: { relations: [{ evidence_id: gapId, hypothesis_id: hypothesisIn(request), relation: "weakens" }] } };
      },
      () => ({ say: "the SIEM is down" }),
    ]);
    await runHunt(harnessOf(specOf(), provider, state), { run_id: RUN, run_kind: "hunt", spec: specOf(), actions: [], queue: new InProcessDirectiveQueue() });

    const projection = fold((await state.read(RUN)) as readonly HuntEvent[]);
    expect(projection.evidence.get(gapId)?.provenance).toBe("tool_failure");
    expect(projection.links.filter((link) => link.evidence_id === gapId && link.relation !== "neither")).toHaveLength(0);
    const ruled = (await state.read(RUN)).find((event) => event.kind === "call" && (event.payload as { tool: string }).tool === "rule_evidence");
    expect((ruled?.payload as { result: string }).result).toMatch(/blind spot/);
  });
});

describe("an adjudication run by one agent", () => {
  it("cannot escalate, and concludes naming the workflow it would have run", async () => {
    const state = new InProcessState<HuntKinds>();
    const provider = modelOf([
      (request) => ({ tool: "abandon_hypothesis", args: { hypothesis_id: hypothesisIn(request), reason: "x", evidence_ids: [] } }),
      () => ({ tool: "conclude_hunt", args: { reason: "I agree with intake", proposed_workflow: "incident-response" } }),
      () => ({ say: "done" }),
    ]);
    await runHunt(harnessOf(specOf("adjudicate"), provider, state), {
      run_id: RUN,
      run_kind: "adjudicate",
      spec: specOf("adjudicate"),
      actions: [],
      queue: new InProcessDirectiveQueue(),
    });

    const offered = provider.requests[0]?.tools.map((tool) => tool.id) ?? [];
    expect(offered).toContain("conclude_hunt");
    expect(offered).not.toContain("handoff_ir");
    const events = (await state.read(RUN)) as readonly HuntEvent[];
    expect(events[0]?.run_kind).toBe("adjudicate");
    // An abandon with nothing attested behind it is refused, as it always was.
    const abandoned = events.find((event) => event.kind === "call" && (event.payload as { tool: string }).tool === "abandon_hypothesis");
    expect((abandoned?.payload as { ok: boolean }).ok).toBe(false);
  });
});
