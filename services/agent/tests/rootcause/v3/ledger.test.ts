// The ledger holds no graph: a reader and a resume fold it back from the alert, the rare
// sweep and each turn's patch. These hold that fold to the live run it was journaled from.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import type { AgentEvent } from "../../../contracts/events.js";
import { defaultConfig, type AgentConfig } from "../../../plugins/rootcause/v3/config.js";
import { applyPatch, createGraph, emptyPatch, type Graph, type GraphPatch, type Prediction } from "../../../plugins/rootcause/v3/graph.js";
import { engineOf, fold, type V3Kinds } from "../../../plugins/rootcause/v3/ledger.js";
import { rootCauseV3Projection, rootCauseV3Replay } from "../../../plugins/rootcause/v3/projection.js";
import { run, seedFromAlert } from "../../../plugins/rootcause/v3/orchestrator.js";
import { domainsIn } from "../../../plugins/rootcause/v3/survey.js";
import { Registry } from "../../../plugins/rootcause/v3/tools/registry.js";
import { splunkTool } from "../../../plugins/rootcause/v3/tools/splunk-tool.js";
import { MemoryTrace } from "../../../plugins/rootcause/v3/trace.js";
import type { Alert } from "../../../plugins/rootcause/v3/types.js";
import { Cassette, SpendingScript } from "./support.js";

const fx = (f: string) => new URL(`./fixtures/mini/${f}`, import.meta.url).pathname;
const range: [string, string] = ["2026-05-05T06:00:00Z", "2026-05-05T18:00:00Z"];
const survey = readFileSync(fx("survey.md"), "utf8");
const domains = domainsIn(survey);
const alert: Alert = JSON.parse(readFileSync(fx("alert.json"), "utf8"));

function script(): unknown[] {
  const entries = JSON.parse(readFileSync(fx("script.json"), "utf8"));
  entries.push({ move: "conclude", correlationId: "N1", toolCalls: [], submission: { conclusion: "A mail dropped the tool that deleted the backup catalog.", chain: ["N4", "N3", "N2", "N1"], rootCause: "N4", openQuestions: ["who sent the mail"], reasoning: "from the graph" } });
  return entries;
}

// A journal as the engine writes one: setup, then a turn event per onTurn, each JSON-round-tripped as the store would.
function journal(cfg: AgentConfig) {
  const events: AgentEvent<V3Kinds>[] = [];
  let seq = 0;
  const push = (kind: string, payload: unknown) =>
    events.push({ run_id: "r1", run_kind: "root_cause", seq: seq++, ts: new Date().toISOString(), kind, payload: JSON.parse(JSON.stringify(payload)), schema_version: 1 } as AgentEvent<V3Kinds>);
  push("run", { run_kind: "root_cause", spec: { sections: { engine: "rootcause-v3" } } });
  push("rootcause_setup", { cfg, alert, index: "lab", range, survey: { text: survey, domains, sizes: {} }, environment: null });
  return { events, push };
}

function deps(cfg: AgentConfig, entries: unknown[], trace = new MemoryTrace("r1"), tools = new Registry()) {
  tools.register(splunkTool(new Cassette(fx("cassette.jsonl")), "lab"));
  const budget = { usd: 0 };
  const model = new SpendingScript({ cfg, tools, domains, survey, range }, entries as never, budget);
  return { model, tools, trace, budget, domains, clock: () => new Date() };
}

describe("rootcause v3 ledger", () => {
  const cfg = { ...defaultConfig, maxUsd: 100, maxTheorize: 0 };

  it("folds back the exact graph the live run ended with, and one step per move", async () => {
    const { events, push } = journal(cfg);
    const d = deps(cfg, script());
    let live: Graph | undefined;
    const r = await run(alert, cfg, d, {
      runId: "r1", range, index: "lab",
      onTurn: (g, turn) => { live = g; push("rootcause_turn", { turn, ...(turn === 0 && g.rare !== undefined ? { rare: g.rare } : {}), entries: d.trace.drain() }); },
    });
    push("rootcause_exit", { exit: r.exit, report: r.report, entries: d.trace.drain() });

    const folded = fold(events);
    expect(engineOf(events)).toBe("rootcause-v3");
    expect(folded.exit).toBe("root-cause-found");
    expect(folded.report).toBe(r.report);
    expect(JSON.parse(JSON.stringify(folded.graph))).toEqual(JSON.parse(JSON.stringify(live)));
    expect(folded.entries).toHaveLength(d.trace.chronological().length);

    const moves = d.trace.chronological().filter((e) => e.type === "decision").length;
    expect(folded.steps).toHaveLength(moves);
    expect(folded.steps.at(-1)!.move).toBe("conclude");
    const searched = folded.steps.find((s) => s.queries.length > 0)!;
    expect(searched.queries[0]!.query).toMatch(/index=lab/);
    expect(folded.steps.some((s) => s.added.length > 0 && s.changed.length > 0)).toBe(true);
    // What each turn did, for the console's row: the target's label, what it created, a story count, the conclusion.
    const made = folded.steps.find((s) => s.created.length > 0)!;
    expect(made.created.every((c) => made.added.includes(c.id) && (folded.graph!.nodes[c.id]?.createdTurn ?? folded.graph!.edges[c.id]?.createdTurn) === made.n)).toBe(true);
    expect(folded.steps.find((s) => folded.graph!.nodes[s.targetId] !== undefined)!.target).not.toBe("");
    expect(folded.steps.at(-1)!.concluded).toBe(true);

    // The console's view: the chain symptom first, ending at the root, and the report.
    push("terminal", { outcome: "completed", reason: "the trace ended: root-cause-found" });
    const view = rootCauseV3Projection("r1", events as never);
    expect(view).toMatchObject({ engine: "rootcause-v3", status: "terminal", outcome: "completed", exit: "root-cause-found", report: r.report });
    expect(view.steps.map((s) => s.step_id)).toEqual(r.rootPath!.nodeIds);
    expect(view.steps.at(-1)).toMatchObject({ origin: true, cause_id: null });
    expect(view.steps[0]!.cause_id).toBe(r.rootPath!.nodeIds[1]);
    const replay = rootCauseV3Replay("r1", events as never);
    expect(replay).toMatchObject({ run_kind: "root_cause", engine: "rootcause-v3", moves: folded.steps });
    expect(replay.steps.filter((s) => s.kind === "notice")).toHaveLength(folded.steps.length);
    expect(replay.steps.filter((s) => s.kind === "search").length).toBe(folded.steps.reduce((n, s) => n + s.queries.length, 0));
    expect(replay.steps.filter((s) => s.kind === "step").map((s) => (s as { step_id: string }).step_id)).toEqual(r.rootPath!.nodeIds);
  });

  it("resumes from the fold to the same end as a run that was never interrupted", async () => {
    const whole = deps(cfg, script());
    const uninterrupted = await run(alert, cfg, whole, { runId: "r1", range, index: "lab" });

    // First worker: journals three turns, then loses its lease.
    const { events, push } = journal(cfg);
    const entries = script();
    const first = deps(cfg, entries);
    const lease = new AbortController();
    await expect(
      run(alert, cfg, first, {
        runId: "r1", range, index: "lab", signal: lease.signal,
        onTurn: (g, turn) => {
          push("rootcause_turn", { turn, ...(turn === 0 && g.rare !== undefined ? { rare: g.rare } : {}), entries: first.trace.drain() });
          if (turn === 3) lease.abort(new Error("lost the lease"));
        },
      }),
    ).rejects.toThrow("lost the lease");

    // Second worker: everything from the ledger; the model carries on where the first left off.
    const folded = fold(events);
    expect(folded.lastTurn).toBe(3);
    const used = (first.model as unknown as { used: Set<number> }).used;
    // The choice the first worker carried is not in the ledger: the resumed run's first turn is a choose turn.
    const carried = { move: "choose", correlationId: "N1", toolCalls: [], submission: { move: "strengthen", target: "E1", why: "the link to the cause is suspected" } };
    const rest = [carried, ...entries.filter((_, i) => !used.has(i))];
    const trace = new MemoryTrace("r1", () => new Date(), folded.entries);
    const tools = new Registry();
    const second = deps(cfg, rest, trace, tools);
    for (const e of folded.entries.filter((e) => e.type === "query")) {
      const p = e.payload as { toolCallId: string; input: unknown };
      const out = folded.entries.find((o) => o.type === "tool-output" && (o.payload as { toolCallId: string }).toolCallId === p.toolCallId)?.payload as { output?: string } | undefined;
      tools.note(p.toolCallId, p.input, out?.output ?? "");
    }
    const resumed = await run(alert, cfg, second, { runId: "r1", range, index: "lab", graph: folded.graph!, startTurn: folded.lastTurn + 1 });

    expect(resumed.exit).toBe(uninterrupted.exit);
    expect(resumed.rootPath?.nodeIds).toEqual(uninterrupted.rootPath?.nodeIds);
    // Ids issued after the resume never reuse one the first worker issued.
    const ids = trace.chronological().filter((e) => e.type === "query").map((e) => (e.payload as { toolCallId: string }).toolCallId);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("folds stories, their test results and probes, though the graph keeps the patch's own objects", () => {
    // The live graph holds the stories a patch added by reference, and later turns change them. Each turn is
    // journaled as it ends, as the worker does, so the fold must not see those later changes twice.
    const { events, push } = journal(cfg);
    const live = seedFromAlert(createGraph("r1"), alert, cfg);
    const pred = (id: string): Prediction => ({ id, sourcetype: "edr", expect: "x", spl: "index=lab x", separates: [], next: false, status: "open" });
    const story = (id: string) => ({ id, round: 1, story: id, accountsFor: [], gaps: "", predictions: [pred(`${id}.P1`)], status: "live" as const, support: 0, against: 0 });
    const turns: GraphPatch[][] = [
      [{ ...emptyPatch(), setTheories: [story("R1.H1"), story("R1.H2")] }],
      [{ ...emptyPatch(), predictionResult: { id: "R1.H1.P1", status: "found", result: "seen", toolCallIds: ["r1"], supports: ["R1.H1"], weakens: ["R1.H2"] } }, { ...emptyPatch(), addProbe: pred("PROBE1") }],
      [{ ...emptyPatch(), predictionResult: { id: "PROBE1", status: "found", result: "read", toolCallIds: ["r2"], supports: [], weakens: [] } }, { ...emptyPatch(), addProbe: pred("PROBE2") }],
      [{ ...emptyPatch(), setTheories: [story("R2.H1")] }],
    ];
    turns.forEach((patches, i) => {
      const entries = patches.map((patch, j) => {
        applyPatch(live, patch, i + 1, cfg);
        return { id: `T${i}${j}`, ts: "", runId: "r1", turn: i + 1, correlationId: "N1", relatedIds: [], type: "graph-patch" as const, payload: { patch } };
      });
      push("rootcause_turn", { turn: i + 1, entries });
    });

    const folded = fold(events).graph!;
    expect(JSON.parse(JSON.stringify(folded))).toEqual(JSON.parse(JSON.stringify(live)));
    expect(folded.theories!.find((t) => t.id === "R1.H1")).toMatchObject({ support: 1, status: "replaced" });
    expect(folded.theories!.find((t) => t.id === "PROBES")!.predictions.map((p) => p.id)).toEqual(["PROBE1", "PROBE2"]);
  });

  it("reads a search's count wherever its output puts it, and calls a search failed only when it did not answer", () => {
    const { events, push } = journal(cfg);
    const entry = (id: string, type: string, payload: unknown) => ({ id, ts: "", runId: "r1", turn: 1, correlationId: "N1", relatedIds: [], type, payload });
    push("rootcause_turn", {
      turn: 1,
      entries: [
        entry("T1", "decision", { move: "expand", targetId: "N1", reason: "why" }),
        entry("T2", "query", { toolCallId: "r1", input: { spl: "index=lab | stats count by host", purpose: "map hosts" } }),
        entry("T3", "tool-output", { toolCallId: "r1", ok: true, output: "purpose: map hosts\nwindow: 2026-05-05T06:00 → 18:00\n1,204 events matched, 3 results (showing 3 of 3)\nhost=a" }),
        entry("T4", "query", { toolCallId: "r2", input: { spl: "index=lab | stats count by dest_port" } }),
        entry("T5", "tool-output", { toolCallId: "r2", ok: false, output: "refused: mapping searches do not group by destination ports\nmore" }),
        entry("T6", "query", { toolCallId: "r3", input: { spl: "index=lab x" } }),
        entry("T7", "tool-output", { toolCallId: "r3", ok: true, output: "purpose: x\n192 events matched, 0 results (showing 0 of 0)\nNOTE: userName is missing" }),
        entry("T8", "query", { toolCallId: "r4", input: { spl: "index=lab y" } }),
        entry("T9", "tool-output", { toolCallId: "r4", ok: true, output: "something else entirely" }),
      ],
    });
    const [step] = fold(events).steps;
    expect(step!.queries).toEqual([
      { ref: "r1", query: "index=lab | stats count by host", rows: 3, purpose: "map hosts" },
      { ref: "r2", query: "index=lab | stats count by dest_port", failed: true, error: "refused: mapping searches do not group by destination ports" },
      { ref: "r3", query: "index=lab x", rows: 0 },
      { ref: "r4", query: "index=lab y" },
    ]);
    const searches = rootCauseV3Replay("r1", events as never).steps.filter((x) => x.kind === "search");
    expect(searches.map((x) => (x as { failed: boolean }).failed)).toEqual([false, true, false, false]);
    // the count is the results, not the events matched; a text without a count line has none
    expect(searches.map((x) => (x as { rows?: number }).rows)).toEqual([3, undefined, 0, undefined]);
  });

  it("carries a settled prediction and a story count on the step", () => {
    const { events, push } = journal(cfg);
    const entry = (id: string, type: string, payload: unknown) => ({ id, ts: "", runId: "r1", turn: 1, correlationId: "N1", relatedIds: [], type, payload });
    const patch = (p: object) => entry("P", "graph-patch", { patch: { addNodes: [], addEdges: [], addEvidence: [], ...p }, result: { idMap: {}, rejected: [], touched: [], resolvedMarkers: [] } });
    push("rootcause_turn", { turn: 1, entries: [entry("T1", "decision", { move: "theorize", targetId: "N1", reason: "why" }), patch({ setTheories: [] })] });
    push("rootcause_turn", { turn: 2, entries: [entry("T2", "decision", { move: "test", targetId: "R1.P1", reason: "why" }), patch({ predictionResult: { id: "R1.P1", status: "absent", result: "", toolCallIds: [], supports: [], weakens: [] } })] });
    const [theorized, tested] = fold(events).steps;
    expect(theorized).toMatchObject({ theories: 0, created: [] });
    expect(tested).toMatchObject({ prediction: { id: "R1.P1", status: "absent" } });
    expect(tested!.theories).toBeUndefined();
  });

  it("carries a settled prediction and a story count on the step", () => {
    const { events, push } = journal(cfg);
    const entry = (id: string, type: string, payload: unknown) => ({ id, ts: "", runId: "r1", turn: 1, correlationId: "N1", relatedIds: [], type, payload });
    const patch = (p: object) => entry("P", "graph-patch", { patch: { addNodes: [], addEdges: [], addEvidence: [], ...p }, result: { idMap: {}, rejected: [], touched: [], resolvedMarkers: [] } });
    push("rootcause_turn", { turn: 1, entries: [entry("T1", "decision", { move: "theorize", targetId: "N1", reason: "why" }), patch({ setTheories: [] })] });
    push("rootcause_turn", { turn: 2, entries: [entry("T2", "decision", { move: "test", targetId: "R1.P1", reason: "why" }), patch({ predictionResult: { id: "R1.P1", status: "absent", result: "", toolCallIds: [], supports: [], weakens: [] } })] });
    const [theorized, tested] = fold(events).steps;
    expect(theorized).toMatchObject({ theories: 0, created: [] });
    expect(tested).toMatchObject({ prediction: { id: "R1.P1", status: "absent" } });
    expect(tested!.theories).toBeUndefined();
  });
});
