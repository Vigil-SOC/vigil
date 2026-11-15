// One recorded investigate run, on today's plugin, against a candidate model.
// The ledger it writes is in memory. Nothing here reaches remoteDispatch, posts
// a terminal, or inserts a workflow run.

import { randomUUID } from "node:crypto";

import type { SpendPayload } from "./contracts/budget.js";
import type { CallPayload, ContentPayload, FramePayload } from "./contracts/events.js";
import { runAgent, type AgentKinds } from "./core/agent.js";
import { agentProjection, dispositionOf } from "./core/agent-view.js";
import { noAnswers } from "./core/answers.js";
import { noAnnounce } from "./core/checkpoints.js";
import { stable } from "./core/frames.js";
import { nullMemory } from "./core/memory.js";
import type { Provider } from "./core/provider.js";
import type { State, ToolDispatch } from "./core/seams.js";
import { DEFAULT_BUDGETS, DEFAULT_RUNTIME, SpecError, type RunSpec } from "./core/spec.js";
import { InProcessState } from "./core/state.js";
import { harnessFor } from "./harness.js";
import { loadPlugin } from "./plugins/index.js";
import { toolsFrom } from "./tools/remote.js";
import { specOf } from "./worker.js";

const NEVER = "the recorded run never made this call";

export interface ReplayRequest {
  run_id: string;
  provider: string;
  model: string;
}

export type ReplayOutcome = { disposition: string | null; cost_usd: number } | { skipped: string };

export function replayRequest(value: unknown): ReplayRequest {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new SpecError("a replay request is a JSON object");
  }
  const fields = value as Record<string, unknown>;
  const text = (key: string): string => {
    const held = fields[key];
    if (typeof held !== "string" || held === "") throw new SpecError(`${key} must be a string`);
    return held;
  };
  return { run_id: text("run_id"), provider: text("provider"), model: text("model") };
}

// The opening task is the first frame's task text. No frame, or no content for
// its hash, is a skip: the case is not re-read to fill the gap.
function openingTask(events: readonly { kind: string; payload: unknown }[]): string | null {
  const content = new Map<string, string>();
  let hash: string | null = null;
  for (const event of events) {
    if (event.kind === "content") content.set((event.payload as ContentPayload).hash, (event.payload as ContentPayload).text);
    if (event.kind === "frame" && hash === null) hash = (event.payload as FramePayload).task;
  }
  if (hash === null) return null;
  const text = content.get(hash);
  return text === undefined ? null : text;
}

function argumentKey(tool: string, args: unknown): string {
  return `${tool}\0${stable(args)}`;
}

function recordedResults(events: readonly { kind: string; payload: unknown }[]): Map<string, string> {
  const held = new Map<string, string>();
  for (const event of events) {
    if (event.kind !== "call") continue;
    const call = event.payload as CallPayload;
    let args: unknown = call.arguments;
    try {
      args = JSON.parse(call.arguments);
    } catch {
      // Not JSON: only a call that sends this same string can match it.
    }
    const key = typeof args === "string" ? `${call.tool}\0${args}` : argumentKey(call.tool, args);
    if (!held.has(key)) held.set(key, call.result);
  }
  return held;
}

// Local tools run. Anything else, including a response tool, answers from the
// recorded call or comes back unavailable. A match returns the recorded result
// text and does not execute the tool.
function recordedDispatch(inner: ToolDispatch, recorded: Map<string, string>, response: ReadonlySet<string>): ToolDispatch {
  return {
    invoke: (tool, args, signal, agentId, approval, callId) => {
      if (tool.local && !response.has(tool.id)) return inner.invoke(tool, args, signal, agentId, approval, callId);
      const result = recorded.get(argumentKey(tool.id, args));
      if (result !== undefined) return Promise.resolve({ ok: true, rows: [result], rowCount: 1, capped: false, sourceSystem: "recorded" });
      return Promise.resolve({ ok: false, failure: { kind: "unavailable", detail: NEVER } });
    },
  };
}

function costUsd(events: readonly { kind: string; payload: unknown }[]): number {
  let cost = 0;
  for (const event of events) {
    if (event.kind !== "spend") continue;
    const spent = (event.payload as SpendPayload).cost_usd;
    if (typeof spent === "number") cost += spent;
  }
  return Number(cost.toFixed(6));
}

export async function replayRecordedRun(state: State, request: ReplayRequest, provider?: Provider): Promise<ReplayOutcome> {
  const events = await state.read(request.run_id);
  const task = openingTask(events);
  if (task === null) return { skipped: "the ledger has no frame content to rebuild the opening task" };

  const recorded = await specOf(state, request.run_id);
  if (recorded === null) return { skipped: "the ledger holds no run event" };
  // The kernel parks a tool named in approvals before dispatch, and this ledger
  // has no approval to release it. The set still marks response tools so the
  // wrapper never executes them; the run itself approves nothing.
  const response = new Set(recorded.approvals ?? []);
  const spec: RunSpec = {
    ...recorded,
    model: request.model,
    provider: request.provider,
    budgets: { ...DEFAULT_BUDGETS },
    runtime: { ...DEFAULT_RUNTIME, ...recorded.runtime },
    approvals: [],
  };
  const plugin = loadPlugin("investigate");
  const fresh = new InProcessState<AgentKinds>();
  const built = harnessFor(spec, fresh, nullMemory);
  const harness = {
    ...built,
    memory: nullMemory,
    dispatch: recordedDispatch(built.dispatch, recordedResults(events), response),
    ...(provider === undefined ? {} : { provider }),
  };
  const runId = randomUUID();
  try {
    await runAgent(harness, {
      run_id: runId,
      run_kind: "investigate",
      spec,
      plugin: plugin.manifest,
      hooks: { ...plugin.hooks, brief: async () => task },
      tools: toolsFrom(spec.tools),
      started_by: "replay",
      answers: noAnswers,
      announce: noAnnounce,
      stop: async () => null,
      signal: AbortSignal.timeout(DEFAULT_BUDGETS.max_wall_ms),
    });
  } catch {
    const partial = await fresh.read(runId);
    return { disposition: null, cost_usd: costUsd(partial) };
  }
  const replayed = await fresh.read(runId);
  const disposition = dispositionOf(agentProjection(runId, replayed));
  return { disposition, cost_usd: costUsd(replayed) };
}
