import { createHash } from "node:crypto";
import type { Budget, Refusal } from "../contracts/budget.js";
import type { NewEvent, RunKind } from "../contracts/events.js";
import type { ToolResult } from "../contracts/tool.js";
import type { Message, Provider } from "./provider.js";
import type { Registry } from "./registry.js";
import type { Wrapped } from "./security.js";
import type { Memory, State, ToolDispatch } from "./seams.js";

// A checkpoint a gated call raised, so a run parked on approval can be found by
// class alone. Workflow classes are the workflow's; this one is the harness's.
export const TOOL_APPROVAL = "tool_approval";


export type Status = "completed" | "waiting_approval" | "failed";

// Everything the loop is given rather than builds. Provider and registry sit
// beside the four seams because they are injected on the same terms.
export interface Harness<Kinds extends Record<string, unknown> = Record<never, never>> {
  provider: Provider;
  registry: Registry;
  dispatch: ToolDispatch;
  budget: Budget;
  memory: Memory;
  state: State<Kinds>;
}

export interface TurnConfig {
  run_id: string;
  run_kind: RunKind;
  role: string;
  system: string;
  task: string;
  // null for a conversational role, whose answer is prose: the last text turn is
  // the answer, and there is no second call asking for it again as JSON.
  schema: Record<string, unknown> | null;
  // Turns the caller already holds, seeded before the loop. A stateless client
  // posts the whole conversation each time and the run is one turn of it.
  history?: readonly Message[];
  // The harness's own cap on tool turns, held whatever a workflow asks for.
  max_turns: number;
  // Which tools ask a human first is deployment's answer, not a property of the
  // tool: the same tool needs approval in one deployment and not in another.
  approvals: ReadonlySet<string>;
  // The workflow's vocabulary, for the injection scanner and nothing else. The
  // harness passes it to the scanner and never reads it.
  verbs: readonly string[];
  result_cap: number;
  recall_limit: number;
  // Lets this turn run against a ledger that already holds a terminal. Only for a turn
  // that describes a run rather than continues one -- never one that can reach a tool.
  after_terminal?: boolean;
  signal?: AbortSignal;
}

export interface Attempt {
  tool: string;
  args: string;
  // Structured for the workflow, rendered for the model: a workflow reading rows
  // never parses what was rendered, and rendering stays in one place.
  result: ToolResult;
  wrapped: Wrapped;
}

// tool and args are null when the run parked on a checkpoint the harness did not
// raise, which is the only way it learns of one: the ledger names no call.
export interface Pending {
  checkpoint_id: string;
  tool: string | null;
  args: string | null;
}

export interface Outcome<T> {
  status: Status;
  value: T | null;
  refusal: Refusal | null;
  // Set only when parked: the call the harness stopped at, and the checkpoint a
  // resolution must answer for it to go through.
  pending: Pending | null;
  // True when the tool loop was stopped by the cap rather than by the model, so
  // a workflow knows the answer was reached over a truncated set of calls.
  capped: boolean;
  transcript: Message[];
  calls: Attempt[];
  turns: number;
  rejected: string[];
  reason: string;
  // What this turn spent, tallied by the turn itself: the pool's running total is only
  // this turn's spend when nothing else is spending concurrently.
  cost_usd: number;
}

// The workflow's events for this turn; the harness appends its own as it burns them.
// No position is offered: the store assigns one, so concurrent turns cannot collide.
export async function commitTurn<Kinds extends Record<string, unknown>>(
  state: State<Kinds>,
  runId: string,
  own: readonly NewEvent<Kinds>[],
): Promise<number> {
  return state.append(runId, own);
}

// Derived from the call rather than generated, so a resumed run recognises the
// resolution that answered this exact call and no other.
export function approvalId(runId: string, tool: string, args: string): string {
  return `apr-${createHash("sha256").update(`${runId}\n${tool}\n${args}`).digest("hex").slice(0, 12)}`;
}
