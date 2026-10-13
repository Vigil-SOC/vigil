import type { BudgetLimits, SpendPayload } from "../contracts/budget.js";
import {
  openCheckpoint,
  type CheckpointPayload,
  type ConclusionPayload,
  type OpenCheckpoint,
  type ResolutionPayload,
  type ReviewPayload,
  type RunKind,
  type RunPayload,
  type StepPayload,
  type TerminalPayload,
  type UnboundPayload,
} from "../contracts/events.js";
import { recalledPayloadOf, type RecallPayload } from "../contracts/memory.js";

// What a reader outside this process is told about a kernel run and what each turn
// was shown. One fold for every plugin, since the events are the kernel's own.

interface Envelope {
  kind: string;
  ts: string;
  seq: number;
  payload: unknown;
}

export interface AgentProjection {
  run_id: string;
  run_kind: RunKind | null;
  plugin: string | null;
  budgets: BudgetLimits | null;
  status: "running" | "waiting_approval" | "terminal";
  outcome: TerminalPayload["outcome"] | null;
  reason: string;
  summary: string | null;
  // Model turns that asked for tools, and the calls they made.
  turns: number;
  steps: StepPayload[];
  // Null until a gateway prices something.
  cost_usd: number | null;
  conclusion: ConclusionPayload | null;
  review: ReviewPayload | null;
  open_checkpoint: OpenCheckpoint | null;
  unbound: UnboundPayload[];
  recall: RecallPayload | null;
}

export function agentProjection(runId: string, events: readonly Envelope[]): AgentProjection {
  const steps: StepPayload[] = [];
  const unbound: UnboundPayload[] = [];
  const raised: CheckpointPayload[] = [];
  const answered = new Set<string>();
  let run: Partial<RunPayload> | null = null;
  let terminal: TerminalPayload | null = null;
  let conclusion: ConclusionPayload | null = null;
  let review: ReviewPayload | null = null;
  let cost: number | null = null;

  for (const event of events) {
    switch (event.kind) {
      case "run":
        run = event.payload as Partial<RunPayload>;
        break;
      case "step":
        steps.push(event.payload as StepPayload);
        break;
      case "unbound":
        unbound.push(event.payload as UnboundPayload);
        break;
      case "checkpoint":
        raised.push(event.payload as CheckpointPayload);
        break;
      case "resolution":
        answered.add((event.payload as ResolutionPayload).checkpoint_id);
        break;
      case "spend": {
        const spent = (event.payload as SpendPayload).cost_usd;
        if (spent !== null) cost = (cost ?? 0) + spent;
        break;
      }
      case "conclusion":
        conclusion = event.payload as ConclusionPayload;
        break;
      case "review":
        review = event.payload as ReviewPayload;
        break;
      case "terminal":
        terminal = event.payload as TerminalPayload;
        break;
    }
  }

  const open = raised.find((checkpoint) => !answered.has(checkpoint.checkpoint_id)) ?? null;
  const sections = ((run?.spec as { sections?: Record<string, unknown> } | undefined)?.sections) ?? {};
  return {
    run_id: runId,
    run_kind: run?.run_kind ?? null,
    plugin: typeof sections["plugin"] === "string" ? (sections["plugin"] as string) : null,
    budgets: run?.budgets ?? null,
    status: terminal !== null ? "terminal" : open !== null ? "waiting_approval" : "running",
    outcome: terminal?.outcome ?? null,
    reason: terminal?.reason ?? "",
    summary: terminal?.summary ?? null,
    turns: new Set(steps.map((step) => step.turn)).size,
    steps,
    cost_usd: cost,
    conclusion,
    review,
    open_checkpoint: open === null ? null : openCheckpoint(open),
    unbound,
    recall: recalledPayloadOf(events as never),
  };
}

export interface AgentReplayTurn {
  turn: number;
  // When the turn's first result was journaled.
  at: string;
  thought: string;
  // What the turn's model calls cost, summed from the spend events written since
  // the previous turn's last result. A call nobody priced adds nothing.
  cost_usd: number;
  steps: StepPayload[];
}

export interface AgentReplay {
  run_id: string;
  recall: RecallPayload | null;
  turns: AgentReplayTurn[];
  conclusion: ConclusionPayload | null;
  review: ReviewPayload | null;
}

// What each turn was shown is the steps before it. A resume is rebuilt from exactly
// these, so a reader rebuilding a turn reads what the model read.
export function agentReplay(runId: string, events: readonly Envelope[]): AgentReplay {
  const turns: AgentReplayTurn[] = [];
  let pending = 0;
  let conclusion: ConclusionPayload | null = null;
  let review: ReviewPayload | null = null;
  for (const event of events) {
    if (event.kind === "spend") {
      pending += (event.payload as SpendPayload).cost_usd ?? 0;
      continue;
    }
    if (event.kind === "conclusion") conclusion = event.payload as ConclusionPayload;
    if (event.kind === "review") review = event.payload as ReviewPayload;
    if (event.kind !== "step") continue;
    const step = event.payload as StepPayload;
    let current = turns.at(-1);
    if (current === undefined || current.turn !== step.turn) {
      current = { turn: step.turn, at: event.ts, thought: step.thought ?? "", cost_usd: 0, steps: [] };
      turns.push(current);
    }
    current.cost_usd = Number((current.cost_usd + pending).toFixed(6));
    pending = 0;
    current.steps.push(step);
  }
  return { run_id: runId, recall: recalledPayloadOf(events as never), turns, conclusion, review };
}
