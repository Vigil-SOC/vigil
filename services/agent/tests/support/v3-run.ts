import { ZERO_TOKENS } from "../../contracts/budget.js";
import type { ToolResult } from "../../contracts/tool.js";
import { budgetOf, FRESH, unmeteredQuota, type Seed } from "../../core/budget.js";
import type { Harness } from "../../core/loop.js";
import { nullMemory } from "../../core/memory.js";
import type { Provider, ProviderEvent, TurnRequest } from "../../core/provider.js";
import { registryOf } from "../../core/registry.js";
import type { ToolDispatch } from "../../core/seams.js";
import type { RunSpec } from "../../core/spec.js";
import { InProcessState } from "../../core/state.js";
import type { V3Kinds } from "../../plugins/rootcause/v3/ledger.js";
import { SPLUNK_EXECUTE } from "../../plugins/rootcause/v3/tools/splunk-source.js";

// A v3 run's moving parts with only the model and Splunk stood in for: the ledger, pool, kernel loop and
// evidence tool are real.

export const telemetry = { id: SPLUNK_EXECUTE, kind: "remote", description: "Run a Splunk search.", parameters: { type: "object" } };

export function specOf(over: Partial<RunSpec> = {}): RunSpec {
  return {
    model: "claude-test",
    budgets: { max_calls: 50, max_cost_usd: 10, max_wall_ms: 600_000, max_park_ms: 60_000 },
    runtime: { max_turns: 8, result_cap: 8_000, recall_limit: 5 },
    approvals: [],
    tools: [telemetry],
    sections: {},
    prompt: "A confirmed compromise: host ws-7 beaconed to 203.0.113.9.",
    ...over,
  } as unknown as RunSpec;
}

const envelope = (results: Record<string, unknown>[]): ToolResult => ({ ok: true, rows: [{ success: true, count: results.length, results }], rowCount: 1, capped: false, sourceSystem: "splunk-selfhosted" });

// splunk_execute as the tools router answers: one envelope row. The evidence tool is local and runs in process.
export function splunk(asked: Record<string, unknown>[] = []): ToolDispatch {
  return {
    invoke: async (tool, args): Promise<ToolResult> => {
      if (tool.local) return tool.invoke(args);
      asked.push(args);
      const spl = String(args["spl_query"] ?? "");
      if (spl.includes("eventcount")) return envelope([{ index: "lab", count: "1200" }]);
      if (spl.includes("tstats")) return envelope([{ sourcetype: "edr", count: "1200", first: "1767571200", last: "1767657600" }]);
      return envelope([]);
    },
  };
}

// wait: how long the model takes, so a stop can arrive while it does.
export type Answer = { calls?: readonly { tool: string; args: unknown }[]; content?: string; emit?: unknown; wait?: number };

// Answers each call by what it was asked: the schema of an emission, else the tool turn in hand.
export function modelOf(answer: (request: TurnRequest, n: number) => Answer, model = "scripted/model"): Provider & { requests: TurnRequest[] } {
  const requests: TurnRequest[] = [];
  return {
    model,
    provider_type: "scripted",
    requests,
    stream: async function* (request: TurnRequest): AsyncGenerator<ProviderEvent> {
      requests.push(request);
      const said = answer(request, requests.length);
      if (said.wait !== undefined) await new Promise((done) => setTimeout(done, said.wait));
      if (request.emit !== undefined) yield { type: "text_delta", text: typeof said.emit === "string" ? said.emit : JSON.stringify(said.emit ?? {}) };
      else if (said.content !== undefined) yield { type: "text_delta", text: said.content };
      yield { type: "usage", tokens: ZERO_TOKENS };
      if (request.emit === undefined) for (const [i, call] of (said.calls ?? []).entries()) yield { type: "tool_call", call: { id: `c${requests.length}-${i}`, tool: call.tool, args: JSON.stringify(call.args) } };
    },
  };
}

export const properties = (request: TurnRequest): string[] => Object.keys((request.emit?.["properties"] ?? {}) as Record<string, unknown>);

export function harnessOf(spec: RunSpec, provider: Provider, dispatch: ToolDispatch = splunk(), state = new InProcessState<V3Kinds>(), seed: Seed = FRESH): Harness<V3Kinds> {
  return { provider, registry: registryOf([], {}), dispatch, budget: budgetOf(spec.budgets, unmeteredQuota, Date.now, seed), memory: nullMemory, state };
}

export const framed = (over: Record<string, unknown> = {}) => ({ title: "beacon", observed_at: "2026-01-05T10:00:00Z", source: "network", artifacts: [{ kind: "host", value: "ws-7" }], index: "lab", ...over });
