import type { NewEvent } from "../../contracts/events.js";
import { commitTurn, type Harness, type Outcome, type TurnConfig } from "../../core/loop.js";
import type { Message } from "../../core/provider.js";
import { SpecError, type RoleSpec, type RunSpec } from "../../core/spec.js";
import { streamTurn, type StreamEvent } from "../../core/stream.js";

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

// One turn of a conversation that is itself one run: the ledger carries what the
// turn spent, and the client carries what was said.
export async function* runChat(harness: Harness, options: ChatOptions): AsyncGenerator<StreamEvent<string>, ChatReport> {
  const lead = leadOf(options.spec);
  const turn = turnFor(options, lead);
  if ((await harness.state.latestSeq(options.run_id)) === null) await open(harness, options);

  // A reader who walks away mid-answer does not un-bill the calls already made. The
  // loop writes each spend before yielding it, so there is nothing to compensate.
  const stream = streamTurn<string>(turn, harness);
  for (;;) {
    const next = await stream.next();
    if (next.done) {
      await commitTurn(harness.state, options.run_id, []);
      return reportOf(next.value);
    }
    yield next.value;
  }
}

function leadOf(spec: RunSpec): RoleSpec {
  const lead = spec.roles.lead;
  if (lead === undefined) throw new SpecError(`arch ${spec.arch} declares no lead, so it has nobody to answer with`);
  if (lead.output_schema !== null) throw new SpecError(`arch ${spec.arch} answers in JSON, which is not a thing to say to a person`);
  return lead;
}

function messageOf(turn: Turn): Message {
  if (turn.role === "assistant") return { role: "assistant", content: turn.content, tool_calls: [] };
  return { role: "user", content: turn.content };
}

function reportOf(outcome: Outcome<string>): ChatReport {
  return { status: outcome.status, answer: outcome.value ?? "", reason: outcome.reason, pending: outcome.pending };
}

function turnFor(options: ChatOptions, lead: RoleSpec): TurnConfig {
  const { runtime } = options.spec;
  const { task, history } = conversationOf(options.turns);
  return {
    run_id: options.run_id,
    run_kind: KIND,
    role: "lead",
    ...(options.agent_id === undefined ? {} : { agent_id: options.agent_id }),
    system: lead.prompt,
    task,
    history,
    schema: null,
    max_turns: runtime.max_turns,
    approvals: new Set(options.spec.approvals),
    // No vocabulary: a conversation chooses no action, so the scanner has only
    // its generic patterns to go on and there is no verb to smuggle.
    verbs: [],
    result_cap: runtime.result_cap,
    recall_limit: runtime.recall_limit,
  };
}

type Event = NewEvent<Record<never, never>>;

function event(options: ChatOptions, kind: Event["kind"], payload: Event["payload"]): Event {
  return { run_id: options.run_id, run_kind: KIND, kind, payload };
}

// A conversation is the run, so it opens once and stays open. Nothing writes a
// terminal: the person may always say something else.
async function open(harness: Harness, options: ChatOptions): Promise<void> {
  await harness.state.append(options.run_id, [
    event(options, "run", {
      run_kind: KIND,
      spec: options.spec,
      budgets: harness.budget.limits,
      seed: options.run_id,
      tenant_id: null,
      started_by: options.started_by ?? "console",
    }),
  ]);
}
