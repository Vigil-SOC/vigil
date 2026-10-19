import type { DispatchPayload } from "../../contracts/events.js";
import type { ToolResult } from "../../contracts/tool.js";
import { latestSteps, redact, type NoticePayload, type StepPayload } from "./proof.js";
import { searchOf } from "./projection.js";

// What a root-cause run did, in ledger order: each search it ran, each step it
// recorded, each notice. The lead journals no rationale, so none is carried.
export interface RootCauseReplay {
  run_id: string;
  run_kind: "root_cause";
  // The run event's limits; left out on a ledger that predates them.
  budgets?: { max_calls?: number; max_cost_usd?: number; max_wall_ms?: number };
  steps: RootCauseReplayStep[];
}

// recorded_at is the envelope time. A step's own `at` is when its event happened.
export type RootCauseReplayStep =
  | { kind: "search"; recorded_at: string; tool: string; args: string; rows: number; failed: boolean; failure?: string }
  | ({ kind: "step"; recorded_at: string } & Pick<
      StepPayload,
      "step_id" | "event" | "who" | "at" | "link" | "cause_id" | "origin" | "link_status" | "origin_status"
    >)
  | { kind: "notice"; recorded_at: string; text: string };

const LIMITS = ["max_calls", "max_cost_usd", "max_wall_ms"] as const;

export function rootCauseReplay(runId: string, events: readonly { kind: string; ts: string; payload: unknown }[]): RootCauseReplay {
  // Redacted against the whole ledger, so a name hidden in the final record never shows in an earlier frame.
  const written = latestSteps(events);
  const steps: RootCauseReplayStep[] = [];
  let budgets: RootCauseReplay["budgets"];

  for (const { kind, ts, payload } of events) {
    switch (kind) {
      case "run": {
        const given = (payload as { budgets?: Record<string, unknown> }).budgets;
        const picked = LIMITS.flatMap((key) => (typeof given?.[key] === "number" ? [[key, given[key]] as const] : []));
        if (picked.length > 0) budgets = Object.fromEntries(picked);
        break;
      }
      case "dispatch": {
        const search = searchOf(payload as DispatchPayload);
        if (search === null) break;
        const result = (payload as DispatchPayload).result as ToolResult | undefined;
        const failure = result !== undefined && !result.ok ? result.failure.kind : undefined;
        steps.push({ kind: "search", recorded_at: ts, ...search, ...(failure === undefined ? {} : { failure }) });
        break;
      }
      case "step": {
        const step = payload as StepPayload;
        steps.push({
          kind: "step",
          recorded_at: ts,
          step_id: step.step_id,
          event: redact(step.event, written),
          who: redact(step.who, written),
          at: step.at,
          link: redact(step.link, written),
          cause_id: step.cause_id,
          origin: step.origin,
          link_status: step.link_status,
          origin_status: step.origin_status,
        });
        break;
      }
      case "notice": {
        const text = (payload as NoticePayload).text;
        if (text !== "") steps.push({ kind: "notice", recorded_at: ts, text });
        break;
      }
    }
  }
  return { run_id: runId, run_kind: "root_cause", ...(budgets === undefined ? {} : { budgets }), steps };
}
