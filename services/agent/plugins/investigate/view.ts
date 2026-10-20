import { agentProjection, agentReplay, dispositionOf, heldOf, modelMs, type AgentProjection, type AgentReplay, type Held } from "../../core/agent-view.js";
import type { CallPayload } from "../../contracts/events.js";
import { classOf, recordOf, recordRows, type RecordQuestion, type RecordStanding } from "../../core/record.js";
import { callViews } from "../../core/call-view.js";
import type { EvidenceView } from "../hunt/projection.js";
import type { LeadProjection } from "./legacy/projection.js";
import type { InvestigateReplay } from "./legacy/replay.js";

// A kernel investigate run answers in the lead workflow's shape, with its own fields
// beside it, so nothing that renders investigations has to know which engine ran one.

interface Envelope {
  kind: string;
  ts: string;
  seq: number;
  payload: unknown;
}

// The record in the hunt projection's row shapes, so the console reads both with one reader.
type RecordEvidence = Pick<EvidenceView, "evidence_id" | "iteration" | "source_system" | "summary" | "is_gap" | "gap_detail" | "captured_at" | "bears_on">;

export interface InvestigateRecord {
  hypotheses: RecordStanding[];
  evidence: RecordEvidence[];
  evidence_count: number;
  open_questions: RecordQuestion[];
  held: Held | null;
}

const timed = (ms: number | undefined): { duration_ms?: number } => (ms === undefined ? {} : { duration_ms: ms });

export function investigateView(runId: string, events: readonly Envelope[]): LeadProjection & InvestigateRecord & { engine: "plugin"; kernel: AgentProjection } {
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
  const held = heldOf(kernel);
  if (kernel.conclusion !== null) decisions.push({ action: "CONCLUDE", rationale: summary, worker: null, ...timed(took.get(Math.max(0, ...took.keys()))) });

  return {
    run_id: runId,
    run_kind: "investigate",
    max_cost_usd: kernel.budgets?.max_cost_usd ?? null,
    disposition: dispositionOf(kernel),
    budgets: kernel.budgets,
    status: kernel.status,
    outcome: kernel.outcome,
    reason: kernel.reason,
    iterations: decisions.length,
    dispatched: kernel.calls.length,
    cost_usd: kernel.cost_usd,
    decisions,
    // The answer the run ends on: a held dismissal reads as what it was held as, never as what was asked.
    findings: kernel.conclusion === null ? [] : [{ agent_id: "agent", answer: held === null ? kernel.conclusion.value : { ...kernel.conclusion.value, [held.field]: held.final } }],
    open_checkpoint: kernel.open_checkpoint,
    calls: callViews(
      [...byTurn.entries()].map(([turn, steps]) => ({
        iteration: turn,
        query_intent: steps.find((one) => one.thought !== undefined)?.thought ?? "",
        cost_usd: 0,
        calls: steps.map((one) => ({
          tool: one.tool,
          result: one.result,
          ref: one.ref,
          arguments: one.arguments,
          ...(one.class === undefined ? {} : { class: one.class }),
          ...(one.duration_ms === undefined ? {} : { duration_ms: one.duration_ms }),
        })),
      })),
    ),
    gaps: kernel.calls
      .filter((one) => classOf(one) === "visibility_gap")
      .map((one) => ({ dispatch_id: one.ref, agent_id: "agent", failure_reason: one.failure, query_intent: one.thought ?? "" })),
    unbound: kernel.unbound,
    recall: kernel.recall,
    ...recordView(events, kernel.calls),
    held,
    engine: "plugin",
    kernel,
  };
}

// Evidence is every call some link names, newest first as a hunt's is; neither counts
// the linked rows that take no stance on a hypothesis.
function recordView(events: readonly Envelope[], calls: readonly CallPayload[]): Omit<InvestigateRecord, "held"> {
  const record = recordOf(events);
  const linked = new Set(record.links.map((link) => link.evidence_id));
  const shown = calls.filter((call) => linked.has(call.ref)).reverse();
  const { hypotheses, bears_on, open_questions } = recordRows(record, shown.map((call) => call.ref));
  const at = new Map(events.filter((one) => one.kind === "call").map((one) => [(one.payload as CallPayload).ref, one.ts]));
  const evidence = shown.map((call) => ({
    evidence_id: call.ref,
    iteration: call.turn,
    source_system: call.tool,
    summary: excerpt(call.result),
    is_gap: classOf(call) === "visibility_gap",
    gap_detail: call.failure,
    captured_at: at.get(call.ref) ?? "",
    bears_on: bears_on.get(call.ref) ?? [],
  }));
  return { hypotheses, evidence, evidence_count: evidence.length, open_questions };
}

// The result as a line: the wrapper dropped, whitespace collapsed, cut short.
function excerpt(result: string): string {
  const text = result.replace(/<\/?vigil:tool_result[^>]*>/g, "").replace(/\s+/g, " ").trim();
  return text.length > 240 ? `${text.slice(0, 240)}…` : text;
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
