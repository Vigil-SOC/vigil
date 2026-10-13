import type {
  CallPayload,
  ChildPayload,
  ConclusionPayload,
  ContentPayload,
  FramePayload,
  ReviewPayload,
  TerminalPayload,
} from "../contracts/events.js";
import { hasRecall, recalledNotesOf } from "../contracts/memory.js";
import { renderRecall, type FoldPolicy } from "./context.js";
import { historyFrom } from "./frames.js";
import type { Message, ToolSchema } from "./provider.js";
import { classOf } from "./record.js";
import { contentHash, rebuiltInput } from "./stream.js";

// Replay, from the ledger alone: each model call's input is rebuilt and its hash diffed
// against the one sent. Nothing is called or recalled: that would test today's estate.

interface Envelope {
  kind: string;
  seq: number;
  payload: unknown;
}

export interface FrameCheck {
  seq: number;
  role: string;
  call: number;
  model_id: string;
  // Null when the rebuilt input hashes to the one sent; otherwise what differed.
  mismatch: string | null;
  rebuilt: string | null;
  recorded: string;
}

export interface FrameReplay {
  frames: FrameCheck[];
  reproduced: number;
  // Hashes a frame names that no content event on this ledger holds.
  missing: string[];
  // Whether the recall the run's keyed read journaled renders to the block its
  // frames carried. Null when the run made no keyed read.
  recall_matches: boolean | null;
}

function contentOf(events: readonly Envelope[]): Map<string, string> {
  const held = new Map<string, string>();
  for (const event of events) {
    if (event.kind !== "content") continue;
    const payload = event.payload as ContentPayload;
    held.set(payload.hash, payload.text);
  }
  return held;
}

function callsByRef(events: readonly Envelope[]): Map<string, CallPayload> {
  const held = new Map<string, CallPayload>();
  for (const event of events) if (event.kind === "call") held.set((event.payload as CallPayload).ref, event.payload as CallPayload);
  return held;
}

// The transcript a frame lists, as the stream held it: each turn's thought and
// the calls it asked for, then their results in order.
export function transcriptOf(calls: readonly CallPayload[]): Message[] {
  const messages: Message[] = [];
  let turn: number | null = null;
  let asking: CallPayload[] = [];
  const flush = (): void => {
    if (asking.length === 0) return;
    messages.push({
      role: "assistant",
      content: asking.find((one) => one.thought !== undefined)?.thought ?? "",
      tool_calls: asking.map((one) => ({ id: one.call_id, tool: one.tool, args: one.arguments })),
    });
    for (const one of asking) messages.push({ role: "tool", call_id: one.call_id, content: one.result });
    asking = [];
  };
  for (const call of calls) {
    if (call.turn !== turn) flush();
    turn = call.turn;
    asking.push(call);
  }
  flush();
  return messages;
}

// The input one frame describes, rebuilt. Throws on a hash the ledger cannot answer.
export function inputOf(frame: FramePayload, content: ReadonlyMap<string, string>, calls: ReadonlyMap<string, CallPayload>): { messages: Message[]; input: string; folded: number } {
  const text = (hash: string): string => {
    const held = content.get(hash);
    if (held === undefined) throw new MissingContent(hash);
    if (contentHash(held) !== hash) throw new Error(`content ${hash} does not hash to its own name`);
    return held;
  };
  const listed = frame.transcript.map((ref) => {
    const call = calls.get(ref);
    if (call === undefined) throw new Error(`the frame lists ${ref}, which this ledger never journaled`);
    return call;
  });
  const history = frame.history === null ? [] : historyFrom(text(frame.history));
  const parts = {
    system: text(frame.system),
    tools: frame.tools.map((tool) => JSON.parse(text(tool.hash)) as ToolSchema),
    recall: text(frame.recall),
    task: text(frame.task),
    transcript: [...history, ...transcriptOf(listed)],
    tail: frame.tail === null ? null : text(frame.tail),
    emit: frame.emit === null ? null : (JSON.parse(text(frame.emit)) as Record<string, unknown>),
    policy: frame.fold.policy as FoldPolicy | null,
    ...(frame.fold.ceiling === undefined ? {} : { ceiling: frame.fold.ceiling }),
  };
  return rebuiltInput(parts);
}

class MissingContent extends Error {
  constructor(readonly hash: string) {
    super(`no content event holds ${hash}`);
  }
}

export function replayFrames(events: readonly Envelope[]): FrameReplay {
  const content = contentOf(events);
  const calls = callsByRef(events);
  const missing = new Set<string>();
  const frames: FrameCheck[] = [];
  for (const event of events) {
    if (event.kind !== "frame") continue;
    const frame = event.payload as FramePayload;
    const check: FrameCheck = { seq: event.seq, role: frame.role, call: frame.call, model_id: frame.model_id, mismatch: null, rebuilt: null, recorded: frame.input };
    try {
      const rebuilt = inputOf(frame, content, calls);
      check.rebuilt = rebuilt.input;
      if (rebuilt.input !== frame.input) check.mismatch = "the input rebuilt from the ledger hashes differently from the one sent";
      else if (rebuilt.folded !== frame.fold.folded) check.mismatch = `the rebuild folded ${rebuilt.folded} messages where the call folded ${frame.fold.folded}`;
    } catch (error) {
      if (error instanceof MissingContent) missing.add(error.hash);
      check.mismatch = error instanceof Error ? error.message : String(error);
    }
    frames.push(check);
  }
  return {
    frames,
    reproduced: frames.filter((frame) => frame.mismatch === null).length,
    missing: [...missing],
    recall_matches: recallMatches(events, content),
  };
}

// A keyed read journals its rows, and the agent's frames carry the block they render to,
// so a replay knows the recall it shows is the one the run was shown.
function recallMatches(events: readonly Envelope[], content: ReadonlyMap<string, string>): boolean | null {
  const log = events as never;
  if (!hasRecall(log)) return null;
  const rendered = contentHash(renderRecall(recalledNotesOf(log)));
  const agent = events.filter((event) => event.kind === "frame" && (event.payload as FramePayload).role === "agent");
  if (agent.length === 0) return null;
  return agent.every((event) => (event.payload as FramePayload).recall === rendered && content.has(rendered));
}

// --- trajectories --------------------------------------------------------------

// One model call as a learner reads it, all off the ledger. Rewards are the reader's; the
// signals they are built from (review, declared gaps, outcome) are here.
export interface TrajectoryStep {
  role: string;
  call: number;
  model_id: string;
  input: Message[];
  input_hash: string;
  // The calls this model call asked for, with what each returned and what it was.
  actions: { ref: string; tool: string; arguments: string; result: string; class: string }[];
  // Set on the call that emitted the run's answer.
  answer: Record<string, unknown> | null;
}

export interface Trajectory {
  run_id: string;
  steps: TrajectoryStep[];
  conclusion: ConclusionPayload | null;
  review: ReviewPayload | null;
  terminal: TerminalPayload | null;
  children: ChildPayload[];
  // False when any step's input could not be rebuilt to the hash it was sent with:
  // a trajectory a learner cannot trust to be what the model saw.
  faithful: boolean;
}

export function trajectoryOf(runId: string, events: readonly Envelope[]): Trajectory {
  const content = contentOf(events);
  const calls = callsByRef(events);
  const steps: TrajectoryStep[] = [];
  let faithful = true;
  let conclusion: ConclusionPayload | null = null;
  let review: ReviewPayload | null = null;
  let terminal: TerminalPayload | null = null;
  const children: ChildPayload[] = [];

  // A call's actions are the calls journaled after its frame and before the next
  // frame of the same role.
  const open = new Map<string, TrajectoryStep>();
  for (const event of events) {
    if (event.kind === "frame") {
      const frame = event.payload as FramePayload;
      let input: Message[] = [];
      try {
        const rebuilt = inputOf(frame, content, calls);
        input = rebuilt.messages;
        if (rebuilt.input !== frame.input) faithful = false;
      } catch {
        faithful = false;
      }
      const step: TrajectoryStep = { role: frame.role, call: frame.call, model_id: frame.model_id, input, input_hash: frame.input, actions: [], answer: null };
      steps.push(step);
      open.set(frame.role, step);
    } else if (event.kind === "call") {
      const call = event.payload as CallPayload;
      open.get(call.role ?? "agent")?.actions.push({ ref: call.ref, tool: call.tool, arguments: call.arguments, result: call.result, class: classOf(call) });
    } else if (event.kind === "conclusion") {
      conclusion = event.payload as ConclusionPayload;
      const last = open.get("agent");
      if (last !== undefined && last.actions.length === 0) last.answer = conclusion.value;
    } else if (event.kind === "review") review = event.payload as ReviewPayload;
    else if (event.kind === "terminal") terminal = event.payload as TerminalPayload;
    else if (event.kind === "child") children.push(event.payload as ChildPayload);
  }
  return { run_id: runId, steps, conclusion, review, terminal, children, faithful };
}
