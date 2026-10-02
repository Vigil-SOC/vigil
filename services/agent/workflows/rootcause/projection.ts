import type { SpendPayload } from "../../contracts/budget.js";
import {
  openCheckpoint,
  type AgentEvent,
  type CheckpointPayload,
  type DispatchPayload,
  type OpenCheckpoint,
  type ResolutionPayload,
  type TerminalPayload,
} from "../../contracts/events.js";
import type { ToolResult } from "../../contracts/tool.js";
import { isProven, latestSteps, type NoticePayload, type RootCauseKinds, type StepPayload } from "./proof.js";

// Searches and argument text are capped: this is polled while the run is in flight.
export const SEARCHES_SHOWN = 50;
const ARGS_SHOWN = 500;

export interface StepView {
  step_id: string;
  event: string;
  who: string;
  at: string;
  link_status: StepPayload["link_status"];
  origin_status: StepPayload["origin_status"];
  proven: boolean;
}

// One search the investigator ran. Never its rows: a long trace would make the
// payload huge, and the rows are the ledger's to hold.
export interface SearchView {
  tool: string;
  arguments: string;
  row_count: number | null;
  failure: string | null;
}

// A live, read-only view for an operator watching a trace. Unlike the report that
// reaches the IR case it is not redacted, so it must never be written there.
export interface RootCauseProjection {
  kind: "root_cause";
  run_id: string;
  status: "running" | "waiting_approval" | "terminal";
  outcome: TerminalPayload["outcome"] | null;
  reason: string;
  // Null until a gateway prices something, as in the lead projection.
  cost_usd: number | null;
  open_checkpoint: OpenCheckpoint | null;
  steps: StepView[];
  searches: SearchView[];
  // The true count; searches above is capped.
  search_count: number;
  notices: string[];
}

export function rootCauseProjection(
  runId: string,
  events: readonly AgentEvent<RootCauseKinds>[],
): RootCauseProjection {
  const raised: CheckpointPayload[] = [];
  const answered = new Set<string>();
  const searches: SearchView[] = [];
  const notices: string[] = [];
  let terminal: TerminalPayload | null = null;
  let cost: number | null = null;

  for (const event of events) {
    switch (event.kind) {
      case "checkpoint":
        raised.push(event.payload as CheckpointPayload);
        break;
      case "resolution":
        answered.add((event.payload as ResolutionPayload).checkpoint_id);
        break;
      case "spend": {
        const spent = (event.payload as SpendPayload).cost_usd;
        if (typeof spent === "number") cost = (cost ?? 0) + spent;
        break;
      }
      case "dispatch": {
        const search = searchOf(event.payload as DispatchPayload);
        if (search !== null) searches.push(search);
        break;
      }
      case "notice": {
        const text = (event.payload as NoticePayload).text;
        if (typeof text === "string") notices.push(text);
        break;
      }
      case "terminal":
        terminal = event.payload as TerminalPayload;
        break;
    }
  }

  const open = raised.find((checkpoint) => !answered.has(checkpoint.checkpoint_id)) ?? null;
  return {
    kind: "root_cause",
    run_id: runId,
    status: terminal !== null ? "terminal" : open !== null ? "waiting_approval" : "running",
    outcome: terminal?.outcome ?? null,
    reason: terminal?.reason ?? "",
    cost_usd: cost,
    open_checkpoint: open === null ? null : openCheckpoint(open),
    steps: latestSteps(events).map(stepView),
    searches: searches.slice(-SEARCHES_SHOWN),
    search_count: searches.length,
    notices,
  };
}

function stepView(step: StepPayload): StepView {
  return {
    step_id: step.step_id,
    event: step.event,
    who: step.who,
    at: step.at,
    link_status: step.link_status,
    origin_status: step.origin_status,
    proven: isProven(step),
  };
}

// A dispatch without a named tool is not a search: gated-call approvals journal one
// with no calls, and a ledger older than this view may carry other shapes.
function searchOf(payload: DispatchPayload): SearchView | null {
  const call = Array.isArray(payload.calls) ? payload.calls[0] : undefined;
  if (call === null || typeof call !== "object") return null;
  const { tool, arguments: args } = call as { tool?: unknown; arguments?: unknown };
  if (typeof tool !== "string" || tool === "") return null;
  const result = payload.result as ToolResult | undefined;
  return {
    tool,
    arguments: (typeof args === "string" ? args : "").slice(0, ARGS_SHOWN),
    row_count: result?.ok === true ? result.rowCount : null,
    failure: payload.status === "failed" ? (payload.failure_reason ?? "failed") : null,
  };
}
