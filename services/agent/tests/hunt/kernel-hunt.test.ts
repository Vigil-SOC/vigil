import { describe, expect, it } from "vitest";
import { archFor } from "../../plugins/kinds.js";
import type { TerminalHandoff } from "../../contracts/events.js";
import { InProcessState } from "../../core/state.js";
import { InProcessDirectiveQueue } from "../../plugins/hunt/directives.js";
import { huntDistil } from "../../plugins/hunt/distil.js";
import type { HuntEvent, HuntKinds } from "../../plugins/hunt/ledger.js";
import { fold } from "../../plugins/hunt/ledger.js";
import { huntProjection } from "../../plugins/hunt/projection.js";
import { runHunt } from "../../plugins/hunt/workflow.js";
import { huntKernelReplay, huntView } from "../../plugins/hunt/view.js";

import { RUN, specOf, hypothesisIn, modelOf, estate, harnessOf, finding, shown, type Step } from "../support/kernel-hunt.js";

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
    expect(huntKernelReplay(RUN, events).kernel.turns.length).toBeGreaterThan(0);
    // The console's move panel reads a decision per move, keyed by the id the Moves
    // tab sends, carrying the board as it stood and whether the call reproduced.
    const moveId = view.moves[0]!.decision_id;
    const one = huntKernelReplay(RUN, events, moveId);
    expect(one.decisions.map((decision) => decision.decision_id)).toEqual([moveId]);
    expect(one.decisions[0]!.recorded.hypotheses.length).toBeGreaterThan(0);
    expect(one.decisions[0]!.mismatch).toBeNull();
    expect(one.decisions[0]!.rebuilt).toEqual(one.decisions[0]!.recorded);
    expect(huntKernelReplay(RUN, events).decisions).toHaveLength(view.moves.length);
    // The Checked tab lists what the hunt asked the estate -- not its bookkeeping --
    // as a controller hunt's worker dispatches did.
    expect(view.calls.map((call) => call.tool)).toEqual(["splunk_search", "search_findings"]);
    expect(view.calls.every((call) => call.iteration !== undefined)).toBe(true);
    // Each move carries the model call's wall time for its turn, as a controller move did.
    expect(view.moves.every((move) => typeof move.duration_ms === "number")).toBe(true);
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
  it.each(["hunt", "adjudicate"] as const)("can be flagged as one: %s's record_evidence offers query_failed", async (kind) => {
    const provider = modelOf([() => ({ say: "nothing to do" })]);
    await runHunt(harnessOf(specOf(kind), provider, new InProcessState<HuntKinds>()), { run_id: RUN, run_kind: kind, spec: specOf(kind), actions: [], queue: new InProcessDirectiveQueue() });

    const record = provider.requests[0]?.tools.find((tool) => tool.id === "record_evidence");
    const item = ((record?.parameters["properties"] as Record<string, { items?: { properties?: Record<string, unknown> } }>)["results"])?.items;
    expect(item?.properties).toHaveProperty("query_failed");
  });

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
        return { tool: "link_evidence", args: { links: [{ evidence_id: gapId, hypothesis_id: hypothesisIn(request), stance: "weakens" }] } };
      },
      () => ({ say: "the SIEM is down" }),
    ]);
    await runHunt(harnessOf(specOf(), provider, state), { run_id: RUN, run_kind: "hunt", spec: specOf(), actions: [], queue: new InProcessDirectiveQueue() });

    const projection = fold((await state.read(RUN)) as readonly HuntEvent[]);
    expect(projection.evidence.get(gapId)?.provenance).toBe("tool_failure");
    expect(projection.links.filter((link) => link.evidence_id === gapId && link.relation !== "neither")).toHaveLength(0);
    const ruled = (await state.read(RUN)).find((event) => event.kind === "call" && (event.payload as { tool: string }).tool === "link_evidence");
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
