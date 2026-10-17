// The ledger holds no graph: a reader and a resume fold it back from the alert, the rare
// sweep and each turn's patch. These hold that fold to the live run it was journaled from.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import type { AgentEvent } from "../../../contracts/events.js";
import { Budget } from "../../../plugins/rootcause/v3/budget.js";
import { defaultConfig, type AgentConfig } from "../../../plugins/rootcause/v3/config.js";
import type { Graph } from "../../../plugins/rootcause/v3/graph.js";
import { engineOf, fold, type V3Kinds } from "../../../plugins/rootcause/v3/ledger.js";
import { ScriptedModelClient } from "../../../plugins/rootcause/v3/model/client.js";
import { rootCauseV3Projection, rootCauseV3Replay } from "../../../plugins/rootcause/v3/projection.js";
import { run } from "../../../plugins/rootcause/v3/orchestrator.js";
import { domainsIn } from "../../../plugins/rootcause/v3/survey.js";
import { Registry } from "../../../plugins/rootcause/v3/tools/registry.js";
import { splunkTool } from "../../../plugins/rootcause/v3/tools/splunk-tool.js";
import { MemoryTrace } from "../../../plugins/rootcause/v3/trace.js";
import type { Alert } from "../../../plugins/rootcause/v3/types.js";
import { Cassette } from "./support.js";

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
  const budget = new Budget(cfg.priceInPerM, cfg.priceOutPerM);
  const model = new ScriptedModelClient({ cfg, tools, budget, domains, survey, range }, entries as never);
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

    // The console's view: the chain symptom first, ending at the root, and the report.
    push("terminal", { outcome: "completed", reason: "the trace ended: root-cause-found" });
    const view = rootCauseV3Projection("r1", events as never);
    expect(view).toMatchObject({ engine: "rootcause-v3", status: "terminal", outcome: "completed", exit: "root-cause-found", report: r.report });
    expect(view.steps.map((s) => s.step_id)).toEqual(r.rootPath!.nodeIds);
    expect(view.steps.at(-1)).toMatchObject({ origin: true, cause_id: null });
    expect(view.steps[0]!.cause_id).toBe(r.rootPath!.nodeIds[1]);
    expect(rootCauseV3Replay("r1", events as never).steps).toEqual(folded.steps);
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
    const rest = entries.filter((_, i) => !used.has(i));
    const trace = new MemoryTrace("r1", () => new Date(), folded.entries);
    const tools = new Registry();
    const second = deps(cfg, rest, trace, tools);
    tools.restore(folded.entries.filter((e) => e.type === "query").map((e) => {
      const p = e.payload as { toolCallId: string; input: unknown };
      const out = folded.entries.find((o) => o.type === "tool-output" && (o.payload as { toolCallId: string }).toolCallId === p.toolCallId)?.payload as { output?: string } | undefined;
      return { toolCallId: p.toolCallId, input: p.input, output: out?.output ?? "" };
    }));
    const resumed = await run(alert, cfg, second, { runId: "r1", range, index: "lab", graph: folded.graph!, startTurn: folded.lastTurn + 1 });

    expect(resumed.exit).toBe(uninterrupted.exit);
    expect(resumed.rootPath?.nodeIds).toEqual(uninterrupted.rootPath?.nodeIds);
    // Ids issued after the resume never reuse one the first worker issued.
    const ids = trace.chronological().filter((e) => e.type === "query").map((e) => (e.payload as { toolCallId: string }).toolCallId);
    expect(new Set(ids).size).toBe(ids.length);
  });
});
