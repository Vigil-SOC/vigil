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
import { isProven, latestSteps, redact, type NoticePayload, type RootCauseKinds, type StepPayload } from "./proof.js";

// What a reader is told about a trace while it runs and after: the steps as last
// written and whether each is proven, what the store was asked, and what it cost.
// Not the ledger: the rows a search returned stay there.
export interface RootCauseProjection {
  run_id: string;
  run_kind: "root_cause";
  status: "running" | "waiting_approval" | "terminal";
  outcome: TerminalPayload["outcome"] | null;
  reason: string;
  // Null until a gateway prices something, as the lead projection reports it.
  cost_usd: number | null;
  // The ceiling the run opened with, so a reader can say how much of it is gone.
  max_cost_usd: number | null;
  steps: RootCauseStepView[];
  proven: number;
  notices: string[];
  // Every search the run made; recent_searches carries only the last few.
  searches: number;
  recent_searches: SearchView[];
  open_checkpoint: OpenCheckpoint | null;
}

export type RootCauseStepView = StepPayload & { proven: boolean };

export interface SearchView {
  tool: string;
  args: string;
  rows: number;
  failed: boolean;
}

// Enough to see what the trace is doing now; the count says how much came before.
export const RECENT_SEARCHES = 10;
const ARGS_CHARS = 500;

export function rootCauseProjection(runId: string, events: readonly AgentEvent<RootCauseKinds>[]): RootCauseProjection {
  const notices: string[] = [];
  const searches: SearchView[] = [];
  const raised: CheckpointPayload[] = [];
  const answered = new Set<string>();
  let terminal: TerminalPayload | null = null;
  let cost: number | null = null;
  let ceiling: number | null = null;

  for (const event of events) {
    switch (event.kind) {
      case "run": {
        const limit = (event.payload as { budgets?: { max_cost_usd?: unknown } }).budgets?.max_cost_usd;
        if (typeof limit === "number") ceiling = limit;
        break;
      }
      case "dispatch": {
        const view = searchOf(event.payload as DispatchPayload);
        if (view !== null) searches.push(view);
        break;
      }
      case "notice": {
        const text = (event.payload as NoticePayload).text;
        if (text !== "") notices.push(text);
        break;
      }
      case "spend": {
        const spent = (event.payload as SpendPayload).cost_usd;
        if (spent !== null) cost = (cost ?? 0) + spent;
        break;
      }
      case "checkpoint":
        raised.push(event.payload as CheckpointPayload);
        break;
      case "resolution":
        answered.add((event.payload as ResolutionPayload).checkpoint_id);
        break;
      case "terminal":
        terminal = event.payload as TerminalPayload;
        break;
    }
  }

  // The report's rule holds here too: a name shows only on a proven step.
  const written = latestSteps(events);
  const steps = written.map((step) => ({
    ...step,
    event: redact(step.event, written),
    who: redact(step.who, written),
    link: redact(step.link, written),
    artifact: redact(step.artifact, written),
    proven: isProven(step),
  }));
  const open = raised.find((checkpoint) => !answered.has(checkpoint.checkpoint_id)) ?? null;
  return {
    run_id: runId,
    run_kind: "root_cause",
    status: terminal !== null ? "terminal" : open !== null ? "waiting_approval" : "running",
    outcome: terminal?.outcome ?? null,
    reason: terminal?.reason ?? "",
    cost_usd: cost,
    max_cost_usd: ceiling,
    steps,
    proven: steps.filter((step) => step.proven).length,
    notices,
    searches: searches.length,
    recent_searches: searches.slice(-RECENT_SEARCHES),
    open_checkpoint: open === null ? null : openCheckpoint(open),
  };
}

// record and finish are the trace's own tools, journaled as steps, not searches.
function searchOf(payload: DispatchPayload): SearchView | null {
  const call = Array.isArray(payload.calls) ? payload.calls[0] : undefined;
  if (call === null || typeof call !== "object") return null;
  const record = call as { tool?: unknown; arguments?: unknown };
  const tool = typeof record.tool === "string" ? record.tool : "";
  if (tool === "" || tool === "record" || tool === "finish") return null;
  const args = typeof record.arguments === "string" ? record.arguments : "";
  const result = payload.result as ToolResult | undefined;
  if (result === undefined || !result.ok) return { tool, args: clip(args), rows: 0, failed: true };
  const rows = result.rows.flatMap(eventsIn);
  const failed = rows.length === 0 && result.rows.some(isErrorRow);
  return { tool, args: clip(args), rows: rows.length, failed };
}

// The splunk envelope, `{count, query, results}`, carries its events in `results`.
// A failure comes back ok, as one `{error, query}` row, and is not an event.
function eventsIn(row: unknown): unknown[] {
  if (isErrorRow(row)) return [];
  if (row !== null && typeof row === "object" && !Array.isArray(row)) {
    const results = (row as Record<string, unknown>)["results"];
    if (Array.isArray(results)) return results;
  }
  return [row];
}

function isErrorRow(row: unknown): boolean {
  if (row === null || typeof row !== "object" || Array.isArray(row)) return false;
  const record = row as Record<string, unknown>;
  return typeof record["error"] === "string" && Object.keys(record).every((key) => key === "error" || key === "query" || key === "spl" || key === "success");
}

function clip(text: string): string {
  return text.length > ARGS_CHARS ? `${text.slice(0, ARGS_CHARS)}…` : text;
}
