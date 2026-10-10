import { agentProjection, agentReplay, modelMs, type AgentProjection, type AgentReplay } from "../../core/agent-view.js";
import { RECORD_TOOLS } from "../../core/record.js";
import { callViews } from "../../core/call-view.js";
import { HUNT_TOOLS, operatorOf } from "./hooks.js";
import { recalledNotesOf } from "../../contracts/memory.js";
import { replayFrames } from "../../core/replay.js";
import { fold } from "./ledger.js";
import type { ReplayReport, ReplayedDecision } from "./replay.js";
import type { Digest } from "./types.js";
import type { CallPayload } from "../../contracts/events.js";
import type { HuntEvent } from "./ledger.js";
import { huntProjection, MOVES_SHOWN, type HuntProjection, type MoveView, type ProjectionOptions } from "./projection.js";

// A kernel hunt writes the controller's record, so the projection folds it unchanged. It
// has no decision events, so its moves are read off the agent's calls under the same verbs.

const VERB: Readonly<Record<string, string>> = {
  record_evidence: "INVESTIGATE",
  link_evidence: "INVESTIGATE",
  propose_hypothesis: "INVESTIGATE",
  validate_hypothesis: "VALIDATE",
  abandon_hypothesis: "ABANDON",
  handoff_ir: "HANDOFF_IR",
  ask_operator: "CHECKPOINT",
  conclude_hunt: "CONCLUDE",
  expand: "EXPAND",
};

// Bookkeeping, not a move: reading the board or a playbook changes nothing.
const QUIET = new Set(["hunt_status", "load_skill", "close_question", "open_question"]);

// The hunt's own record keeping: not a question asked of the estate.
const BOOKKEEPING = new Set<string>([...HUNT_TOOLS, ...RECORD_TOOLS, "load_skill", "expand"]);

interface Envelope {
  kind: string;
  ts: string;
  seq: number;
  payload: unknown;
}

export function huntView(
  runId: string,
  events: readonly Envelope[],
  options: ProjectionOptions = {},
): HuntProjection & { engine: "plugin"; kernel: AgentProjection } {
  const base = huntProjection(runId, events as readonly HuntEvent[], options);
  const took = modelMs(events);
  const moves = events
    .filter((event) => event.kind === "call")
    .map((event) => ({ call: event.payload as CallPayload, at: event.ts }))
    .filter(({ call }) => !QUIET.has(call.tool))
    .map(({ call, at }) => moveOf(call, at, took.get(call.turn)))
    .reverse()
    .slice(0, MOVES_SHOWN);
  // The Checked tab: what the hunt asked the estate, turn by turn, at each turn's cost. A
  // kernel hunt has no workers, so these are its own calls, bookkeeping left out.
  const calls = callViews(
    agentReplay(runId, events).turns.map((turn) => ({
      iteration: turn.turn,
      query_intent: turn.thought,
      cost_usd: turn.cost_usd,
      calls: turn.calls.filter((call) => !BOOKKEEPING.has(call.tool)).map((call) => ({ tool: call.tool, result: call.result, ...(call.duration_ms === undefined ? {} : { duration_ms: call.duration_ms }) })),
    })),
  );
  return { ...base, moves, calls, engine: "plugin", kernel: agentProjection(runId, events) };
}

// A kernel hunt's replay in the controller's shape, one decision per move, each carrying
// the board as the ledger held it then. "Rebuilt" means the model call replayed exactly.
export function huntKernelReplay(runId: string, events: readonly Envelope[], decisionId: string | null = null): ReplayReport & { engine: "plugin"; kernel: AgentReplay } {
  const kernel = agentReplay(runId, events);
  const took = modelMs(events);
  const costOfTurn = new Map(kernel.turns.map((turn) => [turn.turn, turn.cost_usd]));
  const checked = new Map(
    replayFrames(events)
      .frames.filter((frame) => frame.role === "agent")
      .map((frame) => [frame.call, frame.mismatch]),
  );
  const decisions: ReplayedDecision[] = [];
  for (const [index, event] of events.entries()) {
    if (event.kind !== "call") continue;
    const call = event.payload as CallPayload;
    if (QUIET.has(call.tool) || call.role !== undefined) continue;
    if (decisionId !== null && call.ref !== decisionId) continue;
    const board = boardAt(events.slice(0, index), call);
    const mismatch = checked.get(call.turn);
    const reproduced = mismatch === null;
    const args = parsed(call.arguments);
    decisions.push({
      decision_id: call.ref,
      iteration: call.turn,
      action: VERB[call.tool] ?? "INVESTIGATE",
      target: typeof args["hypothesis_id"] === "string" ? (args["hypothesis_id"] as string) : null,
      cost_usd: costOfTurn.get(call.turn) ?? 0,
      ...(took.get(call.turn) === undefined ? {} : { duration_ms: took.get(call.turn) as number }),
      exact: reproduced,
      rebuilt: reproduced ? board : null,
      recorded: board,
      mismatch: mismatch === undefined ? null : mismatch,
    });
  }
  const hunt = fold(events as readonly HuntEvent[]).hunt;
  return {
    hunt_id: hunt.hunt_id,
    decisions,
    reproduced: decisions.filter((one) => one.rebuilt !== null).length,
    inexact: decisions.filter((one) => !one.exact).length,
    recalled: recalledNotesOf(events as never),
    engine: "plugin",
    kernel,
  };
}

const EVIDENCE_SHOWN = 8;

function boardAt(before: readonly Envelope[], call: CallPayload): Digest {
  const view = fold(before as readonly HuntEvent[]);
  const evidence = [...view.evidence.values()];
  const shown = evidence.slice(-EVIDENCE_SHOWN);
  const entity = view.hunt.scope["entity"] as { type?: string; value?: string } | undefined;
  const args = parsed(call.arguments);
  return {
    hunt_id: view.hunt.hunt_id,
    hunt_name: view.hunt.name,
    iteration: call.turn,
    narrative: view.hunt.narrative,
    hypotheses: [...view.hypotheses.values()].map((one) => ({ hypothesis_id: one.hypothesis_id, statement: one.statement, status: one.status })),
    recent_evidence: shown.map((one) => ({
      evidence_id: one.evidence_id,
      source_system: one.source_system,
      summary: one.summary,
      salience: one.salience,
      why_notable: one.why_notable,
      instruction_like: one.instruction_like,
    })),
    weakens: {},
    entities: [],
    focus: {
      entity: entity?.type !== undefined && entity.value !== undefined ? `${entity.type}:${entity.value}` : null,
      hypothesis: typeof args["hypothesis_id"] === "string" ? (args["hypothesis_id"] as string) : null,
    },
    pivot_candidates: [],
    omitted: { count: evidence.length - shown.length, evidence_ids: evidence.slice(0, evidence.length - shown.length).map((one) => one.evidence_id) },
    expansions: [],
    open_questions: [...view.questions.values()].filter((one) => one.status === "open").map((one) => one.question),
    budget_remaining: {
      iterations: Math.max(0, view.hunt.budgets.max_iterations - view.hunt.iteration),
      cost_usd: Number(Math.max(0, view.hunt.budgets.max_cost_usd - view.hunt.cost_usd).toFixed(4)),
    },
    directives: operatorOf(view),
    notes: [],
  } as Digest;
}

// duration_ms is the model call's wall time for the turn, as a controller move's was.
function moveOf(call: CallPayload, at: string, modelMsForTurn: number | undefined): MoveView {
  const args = parsed(call.arguments);
  const hypothesis = typeof args["hypothesis_id"] === "string" ? (args["hypothesis_id"] as string) : null;
  const rationale = [call.thought, typeof args["reason"] === "string" ? args["reason"] : typeof args["rationale"] === "string" ? args["rationale"] : ""]
    .filter((part) => part)
    .join(" — ");
  return {
    decision_id: call.ref,
    iteration: call.turn,
    action: VERB[call.tool] ?? "INVESTIGATE",
    rationale: rationale as string,
    target_entity: null,
    target_hypothesis_id: hypothesis,
    query_intent: VERB[call.tool] === undefined ? `${call.tool} ${call.arguments}`.slice(0, 500) : "",
    worker_agent_id: null,
    evidence_citations: Array.isArray(args["evidence_ids"]) ? (args["evidence_ids"] as string[]) : [],
    cost_usd: 0,
    rejected_attempts: call.ok ? [] : [call.result.slice(0, 500)],
    ...(modelMsForTurn === undefined ? {} : { duration_ms: modelMsForTurn }),
    created_at: at,
  };
}

function parsed(text: string): Record<string, unknown> {
  try {
    const value: unknown = JSON.parse(text);
    return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}
