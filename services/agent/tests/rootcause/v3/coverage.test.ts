// An empty search refutes an event only when its source records the entity at all (coverage.ts): otherwise it is
// a coverage gap, dropped before the patch, whatever the model labelled it.
import { describe, expect, it } from "vitest";
import { Vetter } from "../../../plugins/rootcause/v3/handlers/common.js";
import { gapNote } from "../../../plugins/rootcause/v3/coverage.js";
import { createGraph } from "../../../plugins/rootcause/v3/graph.js";
import { type ModelClient } from "../../../plugins/rootcause/v3/model/client.js";
import { run } from "../../../plugins/rootcause/v3/orchestrator.js";
import { Registry } from "../../../plugins/rootcause/v3/tools/registry.js";
import type { QueryInput, SearchBackend } from "../../../plugins/rootcause/v3/tools/search.js";
import { splunkTool } from "../../../plugins/rootcause/v3/tools/splunk-tool.js";
import { MemoryTrace } from "../../../plugins/rootcause/v3/trace.js";
import type { Alert } from "../../../plugins/rootcause/v3/types.js";
import { SpendingScript } from "./support.js";
import { apply, cfg, seeded } from "./helpers.js";

const range: [string, string] = ["2026-03-11T00:00:00Z", "2026-03-14T00:00:00Z"];
const EMPTY = "0 events matched, 0 results (showing 0 of 0)";

// Searches the model makes come back empty; the code's probes (`| stats count`) find events only where the sourcetype
// records the value: `holds` maps a sourcetype to the values it has.
function backend(holds: Record<string, string[]>): SearchBackend & { probes: string[] } {
  const probes: string[] = [];
  return {
    probes,
    count: async () => 0,
    search: async (q: QueryInput) => {
      if (!q.spl.includes("| stats count")) return EMPTY;
      probes.push(q.spl);
      const st = /sourcetype="([^"]+)"/.exec(q.spl)![1]!;
      const n = (holds[st] ?? []).some((v) => q.spl.includes(`"${v}"`)) ? 5 : 0;
      return `${n} events matched, 1 results (showing 1 of 1)\n{"count":"${n}"}`;
    },
  };
}

const setup = (holds: Record<string, string[]>) => {
  const b = backend(holds);
  const tools = new Registry().register(splunkTool(b, "lab"));
  const ctx: any = { cfg, range, domains: ["sysmon", "winlog"], index: "lab", tools, trace: new MemoryTrace("t").forTurn(3, "strengthen", "N1"), turn: 3, sizes: {} };
  // Searches the model made this turn, in the given sourcetypes.
  const search = async (st: string) => (await tools.call("splunk_query", { purpose: "expect FYODOR-L events here", spl: `index=lab sourcetype="${st}" host="FYODOR-L"`, earliest: range[0], latest: range[1] }, { allowedDomains: ctx.domains, allDomains: ctx.domains, timeoutMs: 1000, range })).toolCallId;
  return { b, tools, ctx, search };
};
const refutes = (ids: string[]) => [{ summary: "no Sysmon events for the host", strength: "direct" as const, polarity: "refutes" as const, toolCallIds: ids }];
const confidence = (list: Awaited<ReturnType<Vetter["evidence"]>>) => {
  const g = seeded();
  const before = g.nodes["N1"]!.confidence;
  apply(g, { addEvidence: list.map((evidence) => ({ targetId: "N1", evidence })) });
  return [before, g.nodes["N1"]!.confidence];
};

describe("coverage gaps", () => {
  it("drops refuting evidence from empty searches in a source that never records the entity; the node does not move", async () => {
    const { b, ctx, search } = setup({ winlog: ["FYODOR-L"] });
    const id = await search("sysmon");
    const kept = await new Vetter(ctx, []).evidence([...refutes([id]), ...refutes([id])], "sysmon", ["FYODOR-L"]);
    expect(kept).toEqual([]);
    expect(b.probes).toHaveLength(1); // the second item reuses the probe
    const [before, after] = confidence(kept);
    expect(after).toBe(before);
    expect(gapNote(ctx)).toMatch(/sysmon holds no events on FYODOR-L/);
  });

  it("keeps it, and it lowers the node, when the source does record the entity", async () => {
    const { ctx, search } = setup({ sysmon: ["FYODOR-L"] });
    const kept = await new Vetter(ctx, []).evidence(refutes([await search("sysmon")]), "sysmon", ["FYODOR-L"]);
    expect(kept).toHaveLength(1);
    const [before, after] = confidence(kept);
    expect(after).toBeLessThan(before!);
    expect(gapNote(ctx)).toBe("");
  });

  it("keeps it when any cited search found rows, when no entity is known, and when the probe fails", async () => {
    const { tools, ctx, search } = setup({});
    const empty = await search("sysmon");
    const v = new Vetter(ctx, []);
    // a cited search with rows
    tools.note("r9", { spl: 'index=lab sourcetype="sysmon" host="FYODOR-L"' }, "3 events matched, 3 results");
    expect(await v.evidence(refutes([empty, "r9"]), "sysmon", ["FYODOR-L"])).toHaveLength(1);
    expect(await v.evidence(refutes([empty]), "sysmon", [])).toHaveLength(1);
    // a probe that errors says nothing
    const failing = setup({});
    (failing.tools as any).run = async () => ({ ok: false, error: "splunk down" });
    failing.tools.note("r1", { spl: 'index=lab sourcetype="sysmon" host="FYODOR-L"' }, `purpose: p\n${EMPTY}`);
    expect(await new Vetter(failing.ctx, []).evidence(refutes(["r1"]), "sysmon", ["FYODOR-L"])).toHaveLength(1);
  });

  it("does not take a symptom out through two direct refutes from an uncovered source: the run is not a false positive", async () => {
    const holds = { winlog: ["FYODOR-L"] };
    const b = backend(holds);
    const tools = new Registry().register(splunkTool(b, "lab"));
    const domains = ["sysmon", "winlog"];
    const runCfg = { ...cfg, maxUsd: 100, rareSweep: false };
    const input = (st: string) => ({ purpose: "expect Sysmon events for FYODOR-L; none means it did not happen", spl: `index=lab sourcetype="${st}" host="FYODOR-L"`, earliest: range[0], latest: range[1] });
    const refuted = (calls: string[]) => [{ summary: "no Sysmon record of the stager", strength: "direct", polarity: "refutes", toolCallIds: calls }];
    const budget = { usd: 0 };
    const script = new SpendingScript({ cfg: runCfg, tools, domains, survey: "", range }, [
      { move: "choose", correlationId: "N1", toolCalls: [], submission: { move: "strengthen", target: "N1", why: "unconfirmed" } },
      { move: "strengthen", correlationId: "N1", toolCalls: [{ tool: "splunk_query", input: input("sysmon") }, { tool: "splunk_query", input: input("sysmon") }],
        submission: { evidence: [...refuted(["$0"]), ...refuted(["$1"])], reasoning: "nothing in Sysmon" } },
      { move: "conclude", correlationId: "N1", toolCalls: [], submission: { conclusion: "open", chain: ["N1"], openQuestions: ["is Sysmon deployed on the host"] } },
    ] as any, budget);
    const trace = new MemoryTrace("cov");
    const alert: Alert = { id: "a", source: "sysmon", title: "PowerShell Empire stager on FYODOR-L", observedAt: "2026-03-12T10:00:00Z", artifacts: [{ kind: "host", value: "FYODOR-L" }], raw: {} };
    let g: ReturnType<typeof createGraph> | undefined;
    const r = await run(alert, runCfg, { model: script as ModelClient, tools, trace, budget, domains, clock: () => new Date() }, { runId: "cov", range, index: "lab", onTurn: (graph) => { g = graph; } });
    expect(r.exit).toBe("budget-exhausted");
    expect(g!.nodes["N1"]).toMatchObject({ status: "suspected", confidence: 0.5 });
    expect(b.probes.length).toBeGreaterThan(0);
    expect(trace.chronological().some((e) => e.type === "rejected-evidence" && /coverage gap: sysmon holds no events on FYODOR-L/.test(JSON.stringify(e.payload)))).toBe(true);
  });
});
