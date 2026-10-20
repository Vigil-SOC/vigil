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
  type CallPayload,
  type ChildPayload,
  type FramePayload,
  type TerminalPayload,
  type UnboundPayload,
} from "../contracts/events.js";
import { recalledPayloadOf, type RecallPayload } from "../contracts/memory.js";
import { heldBecause } from "./agent.js";
import { replayFrames, type FrameReplay } from "./replay.js";

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
  calls: CallPayload[];
  // Null until a gateway prices something.
  cost_usd: number | null;
  conclusion: ConclusionPayload | null;
  review: ReviewPayload | null;
  open_checkpoint: OpenCheckpoint | null;
  unbound: UnboundPayload[];
  recall: RecallPayload | null;
}

// What the reviewer guard changed: the answer asked for, the one the run ends on, and why.
export interface Held {
  field: string;
  asked: string;
  final: string;
  reason: string;
}

export function heldOf(kernel: AgentProjection): Held | null {
  const guarded = kernel.review?.guarded;
  const reason = kernel.review === null ? null : heldBecause(kernel.review);
  return guarded === undefined || reason === null ? null : { ...guarded, reason };
}

// The six words a run's conclusion may say its disposition in.
export const DISPOSITIONS = ["malicious", "suspicious", "benign", "false_positive", "inconclusive", "needs_review"] as const;

// The word the run concluded on, as held: a dismissal the guard moved reads as what it was
// moved to. A dismissal no review ever saw (a crash between the two) is not one that stands.
// Null when the run said none, or said a word that is not one of the six.
export function dispositionOf(kernel: AgentProjection): string | null {
  const said = heldOf(kernel)?.final ?? kernel.conclusion?.value["disposition"];
  const word = (DISPOSITIONS as readonly unknown[]).includes(said) ? (said as string) : null;
  return kernel.review === null && (word === "benign" || word === "false_positive") ? "needs_review" : word;
}

export function agentProjection(runId: string, events: readonly Envelope[]): AgentProjection {
  const steps: CallPayload[] = [];
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
      case "call":
        steps.push(event.payload as CallPayload);
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
    calls: steps,
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
  calls: CallPayload[];
}

export interface AgentReplay {
  run_id: string;
  recall: RecallPayload | null;
  turns: AgentReplayTurn[];
  conclusion: ConclusionPayload | null;
  review: ReviewPayload | null;
  // Every model call's input rebuilt from the ledger and diffed against the hash it
  // was sent with. Empty on a ledger written before calls were framed.
  frames: FrameReplay;
  // Judges and subagents, each on a ledger of its own.
  children: ChildPayload[];
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
    if (event.kind !== "call") continue;
    const step = event.payload as CallPayload;
    let current = turns.at(-1);
    if (current === undefined || current.turn !== step.turn) {
      current = { turn: step.turn, at: event.ts, thought: step.thought ?? "", cost_usd: 0, calls: [] };
      turns.push(current);
    }
    current.cost_usd = Number((current.cost_usd + pending).toFixed(6));
    pending = 0;
    current.calls.push(step);
  }
  return {
    run_id: runId,
    recall: recalledPayloadOf(events as never),
    turns,
    conclusion,
    review,
    frames: replayFrames(events),
    children: events.filter((event) => event.kind === "child").map((event) => event.payload as ChildPayload),
  };
}

// How long each model call took, by turn: its frame is written just before the call and
// its spend when the provider reports, so the two stamps bound it.
export function modelMs(events: readonly Envelope[]): Map<number, number> {
  const held = new Map<number, number>();
  let open: { call: number; at: number } | null = null;
  for (const event of events) {
    if (event.kind === "frame" && (event.payload as FramePayload).role === "agent") {
      open = { call: (event.payload as FramePayload).call, at: Date.parse(event.ts) };
    } else if (event.kind === "spend" && open !== null && (event.payload as SpendPayload).role === "agent") {
      const ms = Date.parse(event.ts) - open.at;
      if (Number.isFinite(ms) && ms >= 0) held.set(open.call, ms);
      open = null;
    }
  }
  return held;
}
