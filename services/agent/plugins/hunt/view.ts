import { agentProjection, agentReplay, type AgentProjection, type AgentReplay } from "../../core/agent-view.js";
import type { CallPayload } from "../../contracts/events.js";
import type { HuntEvent } from "../../workflows/hunt/ledger.js";
import { huntProjection, MOVES_SHOWN, type HuntProjection, type MoveView, type ProjectionOptions } from "../../workflows/hunt/projection.js";

// A kernel hunt writes the controller's record, so the projection folds it unchanged. It
// has no decision events, so its moves are read off the agent's calls under the same verbs.

const VERB: Readonly<Record<string, string>> = {
  record_evidence: "INVESTIGATE",
  rule_evidence: "INVESTIGATE",
  validate_hypothesis: "VALIDATE",
  abandon_hypothesis: "ABANDON",
  handoff_ir: "HANDOFF_IR",
  ask_operator: "CHECKPOINT",
  conclude_hunt: "CONCLUDE",
  expand: "EXPAND",
};

// Bookkeeping, not a move: reading the board or a playbook changes nothing.
const QUIET = new Set(["hunt_status", "load_skill", "close_question"]);

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
  const moves = events
    .filter((event) => event.kind === "call")
    .map((event) => ({ call: event.payload as CallPayload, at: event.ts }))
    .filter(({ call }) => !QUIET.has(call.tool))
    .map(({ call, at }) => moveOf(call, at))
    .reverse()
    .slice(0, MOVES_SHOWN);
  return { ...base, moves, engine: "plugin", kernel: agentProjection(runId, events) };
}

export function huntKernelReplay(runId: string, events: readonly Envelope[]): AgentReplay & { engine: "plugin" } {
  return { ...agentReplay(runId, events), engine: "plugin" };
}

function moveOf(call: CallPayload, at: string): MoveView {
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
    ...(call.duration_ms === undefined ? {} : { duration_ms: call.duration_ms }),
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
