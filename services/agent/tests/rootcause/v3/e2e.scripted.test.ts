// Scripted e2e: hand-written model turns + recorded Splunk results on a small synthetic incident
// (mail -> dropped tool -> wbadmin delete catalog). Fully deterministic; asserts on artifacts, not ids.
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { Budget } from "../../../plugins/rootcause/v3/budget.js";
import { defaultConfig } from "../../../plugins/rootcause/v3/config.js";
import type { Graph } from "../../../plugins/rootcause/v3/graph.js";
import { ScriptedModelClient, type ModelClient, type TurnRequest } from "../../../plugins/rootcause/v3/model/client.js";
import { run } from "../../../plugins/rootcause/v3/orchestrator.js";
import { domainsIn } from "../../../plugins/rootcause/v3/survey.js";
import { Registry } from "../../../plugins/rootcause/v3/tools/registry.js";
import { splunkTool } from "../../../plugins/rootcause/v3/tools/splunk-tool.js";
import { MemoryTrace } from "../../../plugins/rootcause/v3/trace.js";
import type { Alert } from "../../../plugins/rootcause/v3/types.js";
import { Cassette, scriptFrom } from "./support.js";

const fx = (f: string) => new URL(`./fixtures/mini/${f}`, import.meta.url).pathname;

describe("e2e scripted (mini)", () => {
  it("finds the root cause through a boundary, exonerates the spoofed identity", async () => {
    const cfg = { ...defaultConfig, maxUsd: 100, maxTheorize: 0 }; // recorded before stories existed
    const survey = readFileSync(fx("survey.md"), "utf8");
    const domains = domainsIn(survey);
    const tools = new Registry().register(splunkTool(new Cassette(fx("cassette.jsonl")), "lab"));
    const budget = new Budget(cfg.priceInPerM, cfg.priceOutPerM);
    const range: [string, string] = ["2026-05-05T06:00:00Z", "2026-05-05T18:00:00Z"];
    const script = new ScriptedModelClient({ cfg, tools, budget, domains, survey, range }, scriptFrom(fx("script.json")));
    const trace = new MemoryTrace("e2e");
    const alert: Alert = JSON.parse(readFileSync(fx("alert.json"), "utf8"));
    const lines: string[] = [];
    let g: Graph | undefined;
    const r = await run(alert, cfg, { model: script, tools, trace, budget, domains, clock: () => new Date() }, { runId: "e2e", range, index: "lab", log: (s) => lines.push(s), onTurn: (graph) => { g = graph; } });

    expect(r.exit, lines.join("\n")).toBe("root-cause-found");
    expect(script.remaining).toBe(0);
    if (!g) throw new Error("the run handed back no graph");
    const graph: Graph = g;
    const path = r.rootPath!.nodeIds.map((id) => graph.nodes[id]!);
    const has = (i: number, kind: string, value: string) => path[i].artifacts.some((a) => a.kind === kind && a.value === value);
    expect(path).toHaveLength(4);
    expect(has(0, "event-id", "E-900")).toBe(true);
    expect(has(1, "process-guid", "PG-2")).toBe(true);
    expect(has(2, "event-id", "F-10")).toBe(true);
    expect(has(3, "event-id", "M-1")).toBe(true);
    expect(path[3].isRoot).toBe(true);

    // The boundary edr -> mail was resolved by a direct file-hash stitch.
    const marker = Object.values(graph.nodes).find((n) => n.kind === "boundary-marker")!;
    expect(marker.resolvedTo).toBe(path[3].id);
    expect(trace.chronological().some((e) => e.type === "stitch" && (e.payload as any).kind === "auto")).toBe(true);

    // Attribution: the system-issued logon wins, the name on the command line is exonerated.
    const attr = Object.values(graph.edges).filter((e) => e.kind === "attributed-to" && e.target === path[1].id);
    const by = (p: string) => attr.find((e) => graph.nodes[e.source].label === p)!;
    expect(by("ad:mallory").status).toBe("confirmed");
    expect(by("ad:alice").status).toBe("ruled-out");

    const report = r.report;
    expect(report).toMatch(/Exonerated identities\*\*\n\n- ad:alice/);
    expect(report).toMatch(/Identity divergence/);
    expect(report).toMatch(/Disposition:\*\* True Positive/);
    // The injected instruction in the mail subject was flagged, and did not change the outcome.
    expect(report).toMatch(/Suspicious content flagged/);
  });

  it("ends with a conclude turn whose verdict goes into the report; every prompt carries the budget", async () => {
    const cfg = { ...defaultConfig, maxUsd: 100, maxTheorize: 0 };
    const survey = readFileSync(fx("survey.md"), "utf8");
    const domains = domainsIn(survey);
    const tools = new Registry().register(splunkTool(new Cassette(fx("cassette.jsonl")), "lab"));
    const budget = new Budget(cfg.priceInPerM, cfg.priceOutPerM);
    const range: [string, string] = ["2026-05-05T06:00:00Z", "2026-05-05T18:00:00Z"];
    const entries = JSON.parse(readFileSync(fx("script.json"), "utf8"));
    entries.push({ move: "conclude", correlationId: "N1", toolCalls: [], submission: { conclusion: "A phishing mail dropped the tool that deleted the backup catalog.", chain: ["N4", "N3", "N2", "N1"], rootCause: "N4", openQuestions: ["who sent the mail"], reasoning: "from the graph" } });
    const script = new ScriptedModelClient({ cfg, tools, budget, domains, survey, range }, entries);
    const prompts: TurnRequest[] = [];
    const spy: ModelClient = { runTurn: (req) => { prompts.push(req); return script.runTurn(req); } };
    const trace = new MemoryTrace("e2e");
    const alert: Alert = JSON.parse(readFileSync(fx("alert.json"), "utf8"));
    const lines: string[] = [];
    const r = await run(alert, cfg, { model: spy, tools, trace, budget, domains, clock: () => new Date() }, { runId: "e2e", range, index: "lab", log: (s) => lines.push(s) });

    expect(r.exit, lines.join("\n")).toBe("root-cause-found");
    expect(script.remaining).toBe(0);
    expect(prompts.at(-1)!.move).toBe("conclude");
    expect(prompts.filter((p) => p.move !== "conclude").every((p) => p.userContent.includes("<budget>"))).toBe(true);
    const report = r.report;
    expect(report).toContain("## Investigator's conclusion");
    expect(report).toContain("deleted the backup catalog");
    expect(report).toContain("- who sent the mail");
  });
});
