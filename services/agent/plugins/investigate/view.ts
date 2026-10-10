import { agentProjection, agentReplay, modelMs, type AgentProjection, type AgentReplay } from "../../core/agent-view.js";
import type { CallPayload } from "../../contracts/events.js";
import { classOf } from "../../core/record.js";
import { callViews } from "../../workflows/call-view.js";
import type { LeadProjection } from "../../workflows/lead/projection.js";
import type { InvestigateReplay } from "../../workflows/lead/replay.js";

// A kernel investigate run answers in the lead workflow's shape, with its own fields
// beside it, so nothing that renders investigations has to know which engine ran one.

interface Envelope {
  kind: string;
  ts: string;
  seq: number;
  payload: unknown;
}

const timed = (ms: number | undefined): { duration_ms?: number } => (ms === undefined ? {} : { duration_ms: ms });

export function investigateView(runId: string, events: readonly Envelope[]): LeadProjection & { engine: "plugin"; kernel: AgentProjection } {
  const kernel = agentProjection(runId, events);
  const byTurn = groupByTurn(kernel.calls);
  const took = modelMs(events);
  const decisions: { action: string; rationale: string; worker: null; duration_ms?: number }[] = [...byTurn.entries()].map(([turn, steps]) => ({
    action: "EXAMINE",
    rationale: steps.find((one) => one.thought !== undefined)?.thought ?? "",
    worker: null,
    ...timed(took.get(turn)),
  }));
  const summary = conclusionSummary(kernel);
  if (kernel.conclusion !== null) decisions.push({ action: "CONCLUDE", rationale: summary, worker: null, ...timed(took.get(Math.max(0, ...took.keys()))) });

  return {
    run_id: runId,
    run_kind: "investigate",
    budgets: kernel.budgets,
    status: kernel.status,
    outcome: kernel.outcome,
    reason: kernel.reason,
    iterations: decisions.length,
    dispatched: kernel.calls.length,
    cost_usd: kernel.cost_usd,
    decisions,
    findings: kernel.conclusion === null ? [] : [{ agent_id: "agent", answer: kernel.conclusion.value }],
    open_checkpoint: kernel.open_checkpoint,
    calls: callViews(
      [...byTurn.entries()].map(([turn, steps]) => ({
        iteration: turn,
        query_intent: steps.find((one) => one.thought !== undefined)?.thought ?? "",
        cost_usd: 0,
        calls: steps.map((one) => ({ tool: one.tool, result: one.result, ...(one.duration_ms === undefined ? {} : { duration_ms: one.duration_ms }) })),
      })),
    ),
    gaps: kernel.calls
      .filter((one) => classOf(one) === "visibility_gap")
      .map((one) => ({ dispatch_id: one.ref, agent_id: "agent", failure_reason: one.failure, query_intent: one.thought ?? "" })),
    unbound: kernel.unbound,
    recall: kernel.recall,
    engine: "plugin",
    kernel,
  };
}

export function investigateKernelReplay(runId: string, events: readonly Envelope[]): InvestigateReplay & { engine: "plugin"; kernel: AgentReplay } {
  const kernel = agentReplay(runId, events);
  const took = modelMs(events);
  const decisions: InvestigateReplay["decisions"] = kernel.turns.map((turn, index) => ({
    iteration: index + 1,
    at: turn.at,
    action: "EXAMINE",
    rationale: turn.thought,
    worker: null,
    ...timed(took.get(turn.turn)),
    cost_usd: turn.cost_usd,
    calls: turn.calls.map((one) => ({ ref: one.ref, tool: one.tool, arguments: one.arguments, result: one.result, ...(one.duration_ms === undefined ? {} : { duration_ms: one.duration_ms }) })),
  }));
  const concluded = events.find((one) => one.kind === "conclusion");
  if (kernel.conclusion !== null && concluded !== undefined) {
    decisions.push({
      iteration: decisions.length + 1,
      at: concluded.ts,
      action: "CONCLUDE",
      rationale: JSON.stringify(kernel.conclusion.value),
      worker: null,
      ...timed(took.get(Math.max(0, ...took.keys()))),
      cost_usd: kernel.conclusion.cost_usd,
      calls: [],
    });
  }
  return { run_id: runId, run_kind: "investigate", decisions, engine: "plugin", kernel };
}

function groupByTurn(steps: readonly CallPayload[]): Map<number, CallPayload[]> {
  const grouped = new Map<number, CallPayload[]>();
  for (const step of steps) grouped.set(step.turn, [...(grouped.get(step.turn) ?? []), step]);
  return grouped;
}

function conclusionSummary(kernel: AgentProjection): string {
  if (kernel.summary !== null && kernel.summary !== "") return kernel.summary;
  const value = kernel.conclusion?.value ?? {};
  return typeof value["summary"] === "string" ? (value["summary"] as string) : "";
}
