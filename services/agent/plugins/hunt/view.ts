import { agentProjection, agentReplay, type AgentProjection, type AgentReplay } from "../../core/agent-view.js";
import type { SpendPayload } from "../../contracts/budget.js";
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
import { HUNT_AGENT, parsedArgs, turnWords } from "./words.js";

// A kernel hunt writes the controller's record, so the projection folds it unchanged. It
// has no decision events, so its moves are read off the agent's calls: one move per model
// turn, the unit its turn limit counts, named for the most consequential call it made.

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
  const moves = turnsOf(events)
    .map(moveOf)
    .reverse()
    .slice(0, MOVES_SHOWN);
  // The Checked tab: what the hunt asked the estate, turn by turn, at each turn's cost. A
  // kernel hunt has no workers, so these are its own calls, bookkeeping left out. A turn
  // nobody priced carries no cost rather than a zero.
  const costs = new Map(turnsOf(events).map((turn) => [turn.turn, turn.cost_usd]));
  // Each call's question is the call in words, not the turn's prose.
  const calls = callViews(
    agentReplay(runId, events).turns.flatMap((turn) =>
      turn.calls
        .filter((call) => !BOOKKEEPING.has(call.tool))
        .map((call) => ({
          iteration: turn.turn,
          query_intent: turnWords([call]).words,
          ...(costs.get(turn.turn) == null ? {} : { cost_usd: costs.get(turn.turn) as number }),
          calls: [
            {
              tool: call.tool,
              result: call.result,
              ref: call.ref,
              arguments: call.arguments,
              ...(call.class === undefined ? {} : { class: call.class }),
              ...(turn.thought ? { thought: turn.thought } : {}),
              ...(call.duration_ms === undefined ? {} : { duration_ms: call.duration_ms }),
            },
          ],
        })),
    ),
  );
  return { ...base, moves, calls, engine: "plugin", kernel: agentProjection(runId, events) };
}

// A kernel hunt's replay in the controller's shape, one decision per model turn, each
// carrying the board as the ledger held it when the turn's first call was made.
// "Rebuilt" means the model call replayed exactly.
export function huntKernelReplay(runId: string, events: readonly Envelope[], decisionId: string | null = null): ReplayReport & { engine: "plugin"; kernel: AgentReplay } {
  const kernel = agentReplay(runId, events);
  const checked = new Map(
    replayFrames(events)
      .frames.filter((frame) => frame.role === "agent")
      .map((frame) => [frame.call, frame.mismatch]),
  );
  const decisions: ReplayedDecision[] = [];
  for (const turn of turnsOf(events)) {
    if (decisionId !== null && turn.ref !== decisionId) continue;
    const move = moveOf(turn);
    const board = boardAt(events.slice(0, turn.index), turn.turn, move.target_hypothesis_id);
    const mismatch = checked.get(turn.turn);
    const reproduced = mismatch === null;
    decisions.push({
      decision_id: turn.ref,
      iteration: turn.turn,
      action: move.action,
      target: move.target_hypothesis_id,
      cost_usd: turn.cost_usd,
      ...(turn.model_ms === null ? {} : { duration_ms: turn.model_ms }),
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

// One model turn of the hunt's own agent: the calls it asked for, what it said first,
// and what its model call cost and took, where the ledger recorded either.
interface Turn {
  turn: number;
  // The first call's ref: what a move and its replayed decision are keyed by.
  ref: string;
  // Where the turn's first call sits in the ledger: the board it was made over is before it.
  index: number;
  at: string;
  thought: string;
  calls: CallPayload[];
  cost_usd: number | null;
  model_ms: number | null;
}

// The agent's model call is framed, then priced, then its calls journaled, so the agent's
// spend and frame-to-spend time since the previous turn are this turn's. A call made under
// another role (a subagent's step) is not the agent's move.
function turnsOf(events: readonly Envelope[]): Turn[] {
  const turns: Turn[] = [];
  let cost: number | null = null;
  let took: number | null = null;
  let framed: number | null = null;
  for (const [index, event] of events.entries()) {
    if (event.kind === "frame" && (event.payload as { role?: string }).role === "agent") {
      framed = Date.parse(event.ts);
      continue;
    }
    if (event.kind === "spend" && (event.payload as SpendPayload).role === "agent") {
      const spent = (event.payload as SpendPayload).cost_usd;
      if (spent !== null) cost = (cost ?? 0) + spent;
      const ms = framed === null ? Number.NaN : Date.parse(event.ts) - framed;
      if (Number.isFinite(ms) && ms >= 0) took = (took ?? 0) + ms;
      framed = null;
      continue;
    }
    if (event.kind !== "call") continue;
    const call = event.payload as CallPayload;
    if (call.role !== undefined) continue;
    const current = turns.at(-1);
    if (current !== undefined && current.turn === call.turn) {
      current.calls.push(call);
      if (!current.thought && call.thought) current.thought = call.thought;
      continue;
    }
    turns.push({
      turn: call.turn,
      ref: call.ref,
      index,
      at: event.ts,
      thought: call.thought ?? "",
      calls: [call],
      cost_usd: cost === null ? null : Number(cost.toFixed(6)),
      model_ms: took,
    });
    cost = null;
    took = null;
  }
  return turns;
}

const EVIDENCE_SHOWN = 8;

function boardAt(before: readonly Envelope[], iteration: number, focus: string | null): Digest {
  const view = fold(before as readonly HuntEvent[]);
  const evidence = [...view.evidence.values()];
  const shown = evidence.slice(-EVIDENCE_SHOWN);
  const entity = view.hunt.scope["entity"] as { type?: string; value?: string } | undefined;
  return {
    hunt_id: view.hunt.hunt_id,
    hunt_name: view.hunt.name,
    iteration,
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
      hypothesis: focus,
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

// One turn as a move. query_intent is the turn in words, never a tool call's JSON;
// cost_usd and duration_ms are the turn's model call, absent where nothing recorded them.
function moveOf(turn: Turn): MoveView {
  const { action, words } = turnWords(turn.calls);
  const args = turn.calls.map((call) => parsedArgs(call.arguments));
  const hypothesis = args.map((one) => one["hypothesis_id"]).find((one): one is string => typeof one === "string") ?? null;
  const said = args.map((one) => one["reason"] ?? one["rationale"]).filter((one): one is string => typeof one === "string" && one.trim() !== "");
  return {
    decision_id: turn.ref,
    iteration: turn.turn,
    action,
    rationale: [turn.thought, ...said].filter((part) => part).join(" — "),
    target_entity: null,
    target_hypothesis_id: hypothesis,
    query_intent: words.slice(0, 500),
    worker_agent_id: HUNT_AGENT,
    evidence_citations: [...new Set(args.flatMap((one) => (Array.isArray(one["evidence_ids"]) ? (one["evidence_ids"] as unknown[]).filter((id): id is string => typeof id === "string") : [])))],
    cost_usd: turn.cost_usd,
    rejected_attempts: turn.calls.filter((call) => !call.ok).map((call) => call.result.slice(0, 500)),
    ...(turn.model_ms === null ? {} : { duration_ms: turn.model_ms }),
    created_at: turn.at,
  };
}
