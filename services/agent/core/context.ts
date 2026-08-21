import type { Message, ToolSchema } from "./provider.js";

// Caching is automatic and prefix-based on the OpenAI surface: there is no
// breakpoint to emit, so a byte-identical prefix is the whole of the mechanism.
export interface Prefix {
  system: string;
  tools: readonly ToolSchema[];
  recall: string;
}

export interface FoldPolicy {
  // The opening frames the run and the recent turns carry its state, so the fold
  // takes the middle and never either edge.
  head: number;
  tail: number;
  max_messages: number;
  // What the whole request may weigh. A count bounds how many turns are carried and
  // says nothing about how heavy one is, so the tail shrinks until the request fits.
  max_chars: number;
}

// 120k characters is roughly 30k tokens of history, which leaves a long answer room
// inside any provider's window and inside the gateway's own per-request ceiling.
export const DEFAULT_FOLD: FoldPolicy = { head: 2, tail: 8, max_messages: 40, max_chars: 120_000 };

export function sizeOf(messages: readonly Message[]): number {
  return messages.reduce((total, message) => total + message.content.length, 0);
}

export type Summarise = (folded: readonly Message[]) => string;

// Sorted at every depth. Two objects that differ only in key order serialise to
// different bytes, which costs the whole prefix for nothing.
export function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (typeof value !== "object" || value === null) return value;
  const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) => (a < b ? -1 : 1));
  return Object.fromEntries(entries.map(([key, held]) => [key, canonical(held)]));
}

// Registration order is whatever the registry happened to do, so it is replaced
// by the one order that is stable across processes.
export function stableTools(tools: readonly ToolSchema[]): readonly ToolSchema[] {
  return [...tools]
    .sort((left, right) => (left.id < right.id ? -1 : 1))
    .map((tool) => ({ ...tool, parameters: canonical(tool.parameters) as Record<string, unknown> }));
}

// Recall is nondeterministic, so it is rendered once and carried. Re-recalling
// per turn moves bytes inside the prefix and never hits cache again.
export function renderRecall(notes: readonly string[]): string {
  if (notes.length === 0) return "";
  return `Recalled from earlier work:\n${notes.map((note) => `- ${note}`).join("\n")}`;
}

export function prefixOf(system: string, tools: readonly ToolSchema[], notes: readonly string[]): Prefix {
  return { system, tools: stableTools(tools), recall: renderRecall(notes) };
}

// What the cache is keyed on. Exposed so a test can assert byte-identity rather
// than assert the shape and hope.
export function prefixBytes(prefix: Prefix): string {
  return JSON.stringify(canonical({ system: prefix.system, tools: prefix.tools, recall: prefix.recall }));
}

export function prefixMessages(prefix: Prefix, task: string): Message[] {
  const opening = prefix.recall === "" ? task : `${task}\n\n${prefix.recall}`;
  return [
    { role: "system", content: prefix.system },
    { role: "user", content: opening },
  ];
}

export interface Folded {
  messages: readonly Message[];
  folded: number;
}

// A tool result cannot outlive the assistant turn that asked for it, so a fold
// that would strand one takes the asking turn with it.
function boundary(history: readonly Message[], from: number): number {
  let at = from;
  while (at < history.length && history[at]?.role === "tool") at += 1;
  return at;
}

// A tail opening on a tool result has lost the turn that asked for it to the summary,
// and the provider refuses the request: every tool_result needs its tool_use.
function opening(history: readonly Message[], from: number): number {
  let at = from;
  while (at > 0 && history[at]?.role === "tool") at -= 1;
  return at;
}

// Folded to the count first, then to the weight: head and tail give ground a turn at a
// time until the request fits max_chars, and every candidate goes through foldToCount so
// the edge rules hold. Neither edge goes below one, so an oversized message is result_cap's job.
export function foldHistory(
  history: readonly Message[],
  summarise: Summarise,
  policy: FoldPolicy = DEFAULT_FOLD,
): Folded {
  let best = foldToCount(history, summarise, policy);
  if (sizeOf(best.messages) <= policy.max_chars) return best;

  for (let tail = policy.tail; tail >= 1; tail -= 1) {
    for (let head = policy.head; head >= 1; head -= 1) {
      // max_messages: 0 so the fold applies however few turns are left: weight decides here.
      const candidate = foldToCount(history, summarise, { ...policy, head, tail, max_messages: 0 });
      // A narrower edge can fold nothing and return the history whole, so keep it only
      // when it is actually lighter.
      if (sizeOf(candidate.messages) < sizeOf(best.messages)) best = candidate;
      if (sizeOf(best.messages) <= policy.max_chars) return best;
    }
  }
  return best;
}

function foldToCount(history: readonly Message[], summarise: Summarise, policy: FoldPolicy): Folded {
  if (history.length <= policy.max_messages) return { messages: history, folded: 0 };

  // The head grows to the boundary rather than the middle starting after it:
  // skipping those messages in both slices would drop them from the context.
  const start = boundary(history, policy.head);
  const head = history.slice(0, start);
  const end = Math.max(start, opening(history, history.length - policy.tail));
  const middle = history.slice(start, end);
  if (middle.length === 0) return { messages: history, folded: 0 };

  const note: Message = { role: "user", content: summarise(middle) };
  return { messages: [...head, note, ...history.slice(end)], folded: middle.length };
}

// Re-rendered every turn and never written to the transcript: the working state
// is volatile, and anything in history is permanent by construction.
export function transientTail(working: string): Message[] {
  return working === "" ? [] : [{ role: "user", content: working }];
}

export function assemble(
  prefix: Prefix,
  task: string,
  history: readonly Message[],
  working: string,
  summarise: Summarise,
  policy: FoldPolicy = DEFAULT_FOLD,
): { messages: Message[]; folded: number } {
  const intro = prefixMessages(prefix, task);
  const tail = transientTail(working);
  // The ceiling is the whole request's, so the parts the fold cannot touch -- the system
  // prompt and the tool catalogue -- are spent before it gets a budget.
  const spent = sizeOf(intro) + sizeOf(tail) + JSON.stringify(prefix.tools).length;
  const room = Math.max(0, policy.max_chars - spent);
  const { messages, folded } = foldHistory(history, summarise, { ...policy, max_chars: room });
  return { messages: [...intro, ...messages, ...tail], folded };
}
