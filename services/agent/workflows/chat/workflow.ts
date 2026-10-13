import { streamAgent, type AgentKinds, type AgentReport } from "../../core/agent.js";
import type { Harness, Outcome } from "../../core/loop.js";
import type { Message } from "../../core/provider.js";
import { SpecError, type RunSpec } from "../../core/spec.js";
import type { StreamEvent } from "../../core/stream.js";
import { loadPlugin } from "../../plugins/index.js";
import { toolsFrom } from "../../tools/remote.js";

const KIND = "chat";

export interface Turn {
  role: "user" | "assistant";
  content: string;
}

export interface ChatOptions {
  run_id: string;
  spec: RunSpec;
  turns: readonly Turn[];
  started_by?: string;
  // The agent this conversation belongs to, when it belongs to one (#1560):
  // its skill reads are credited to it. Absent for a plain chat.
  agent_id?: string;
}

export interface ChatReport {
  status: Outcome<string>["status"];
  answer: string;
  reason: string;
  pending: Outcome<string>["pending"];
}

// Every tool the config declared, because a chat config is assembled per request:
// which tools a conversation may reach is the caller's answer, not the arch's.
export function grantsOf(spec: RunSpec): Record<string, readonly string[]> {
  return { lead: spec.tools.map((tool) => tool.id) };
}

// The client is stateless and posts the whole conversation each turn: the opening
// message is what the prefix caches on, the rest is history the fold may take.
export function conversationOf(turns: readonly Turn[]): { task: string; history: Message[] } {
  const [opening, ...rest] = turns;
  if (opening === undefined) throw new SpecError("a chat turn needs at least one message");
  return { task: opening.content, history: rest.map(messageOf) };
}

// One turn of a conversation that is one run: the chat plugin on the kernel. The
// ledger carries what the turn spent and called, the client what was said.
export async function* runChat(harness: Harness, options: ChatOptions): AsyncGenerator<StreamEvent<string>, ChatReport> {
  const conversation = conversationOf(options.turns);
  const { manifest, hooks } = loadPlugin("chat");
  // A reader who walks away mid-answer does not un-bill the calls already made. The
  // loop writes each spend before yielding it, so there is nothing to compensate.
  const stream = streamAgent(harness as unknown as Harness<AgentKinds>, {
    run_id: options.run_id,
    run_kind: KIND,
    spec: options.spec,
    plugin: manifest,
    ...(hooks === undefined ? {} : { hooks }),
    tools: toolsFrom(options.spec.tools),
    started_by: options.started_by ?? "console",
    conversation,
    ...(options.agent_id === undefined ? {} : { agent_id: options.agent_id }),
  });
  for (;;) {
    const next = await stream.next();
    if (next.done) return reportOf(next.value);
    yield next.value as StreamEvent<string>;
  }
}

function messageOf(turn: Turn): Message {
  if (turn.role === "assistant") return { role: "assistant", content: turn.content, tool_calls: [] };
  return { role: "user", content: turn.content };
}

function reportOf(report: AgentReport): ChatReport {
  const answer = typeof report.value === "string" ? report.value : "";
  return { status: report.status === "waiting_approval" ? "waiting_approval" : report.status === "completed" ? "completed" : "failed", answer, reason: report.reason, pending: report.pending };
}
