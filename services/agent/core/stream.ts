// Named rather than default: ajv is CJS, so under the image build's NodeNext
// resolution a default import lands on module.exports.default. The named export
// exists in both, so this one line satisfies `bundler` and NodeNext alike.
import { Ajv, type ValidateFunction } from "ajv";
import { GatewayExhausted } from "./limiter.js";
import { ZERO_TOKENS, type Refusal, type SpendPayload, type TokenCounts } from "../contracts/budget.js";
import type { CheckpointPayload, DispatchPayload, NewEvent, ResolutionPayload, TerminalPayload } from "../contracts/events.js";
import { ToolBoundsViolation, type RegisteredTool, type ToolResult } from "../contracts/tool.js";
import {
  approvalId,
  TOOL_APPROVAL,
  type Attempt,
  type Harness,
  type Outcome,
  type Pending,
  type Status,
  type TurnConfig,
} from "./loop.js";
import { ProviderError, type Message, type ToolCall, type ToolSchema, type Turn, type TurnRequest } from "./provider.js";
import { assemble, prefixOf, type FoldPolicy, type Prefix } from "./context.js";
import { scannerFor, wrap } from "./security.js";
import type { State } from "./seams.js";

// What a run reports as it happens. The first three are the provider's, relayed;
// the rest are the harness's, and a run ends on exactly one of the last three.
export type StreamEvent<T = unknown> =
  | { type: "text_delta"; text: string }
  | { type: "usage"; payload: SpendPayload }
  | { type: "tool_call"; call: ToolCall }
  | { type: "tool_result"; call: ToolCall; attempt: Attempt }
  | { type: "folded"; folded: number; remaining: number }
  | { type: "approval_required"; pending: Pending }
  | { type: "done"; outcome: Outcome<T> }
  | { type: "failed"; outcome: Outcome<T> };

export type TurnStream<T> = AsyncGenerator<StreamEvent<T>, Outcome<T>>;

// Generic over the workflow's kinds only so any workflow fits. The loop appends
// none of them and reads only the domain-free set.
export function streamTurn<T, Kinds extends Record<string, unknown> = Record<never, never>>(
  cfg: TurnConfig,
  harness: Harness<Kinds>,
): TurnStream<T> {
  return new Run<T, Kinds>(cfg, harness).execute();
}

// For a caller that wants the outcome and nothing that happened on the way to it.
export async function drain<T>(stream: TurnStream<T>): Promise<Outcome<T>> {
  for (;;) {
    const next = await stream.next();
    if (next.done) return next.value;
  }
}

class Run<T, Kinds extends Record<string, unknown>> {
  private readonly scan: ReturnType<typeof scannerFor>;
  private readonly tools: readonly RegisteredTool[];
  private readonly calls: Attempt[] = [];
  private readonly rejected: string[] = [];
  private readonly transcript: Message[] = [];
  private prefix: Prefix = { system: "", tools: [], recall: "" };
  private lastFold = 0;
  // Null until a write-up dies: retrying the largest request a role can send, unchanged,
  // is three times the cost for the same answer, so the retry sends less.
  private tightened: FoldPolicy | null = null;
  private folds = 0;
  private turns = 0;
  private capped = false;
  private spent = 0;
  private prose = "";
  private readonly folder: Folder<Kinds>;

  constructor(
    private readonly cfg: TurnConfig,
    private readonly harness: Harness<Kinds>,
  ) {
    this.scan = scannerFor(cfg.verbs);
    this.tools = harness.registry.granted(cfg.role);
    this.folder = new Folder(harness.state, cfg.run_id);
  }

  // A provider that dies is a run that failed, which is what Outcome is for: it carries
  // the status, the reason and the calls already made, where a thrown error loses all
  // three. An abort still throws, because a cancelled run is not a run that answered.
  async *execute(): TurnStream<T> {
    try {
      return yield* this.attempt();
    } catch (error) {
      if (this.cfg.signal?.aborted === true || hardStop(error)) throw error;
      const reason = error instanceof Error ? error.message : String(error);
      return yield* announce(this.done("failed", null, reason));
    }
  }

  private async *attempt(): TurnStream<T> {
    this.transcript.push(...(this.cfg.history ?? []));

    // Recalled once and rendered into the opening turn, never re-recalled per
    // tool turn: a prefix that changes mid-loop is a prefix that cannot cache.
    const recalled = await this.harness.memory.recall(this.cfg.task, this.cfg.recall_limit);
    this.prefix = prefixOf(this.cfg.system, this.tools.map(schemaOf), recalled);

    const schema = this.cfg.schema;
    const ended = yield* this.toolLoop();
    if (ended !== null) return yield* announce(ended);
    // Prose was already streamed as it arrived, so asking for it again would bill
    // a second call to be told the same thing.
    if (schema === null) return yield* announce(this.done("completed", this.prose as T, "the role answered"));
    return yield* announce(yield* this.emit(schema));
  }

  // Returns an outcome only when the run ends here; otherwise the loop stops
  // because the model asked for no tools or because the cap stopped it.
  private async *toolLoop(): AsyncGenerator<StreamEvent<T>, Outcome<T> | null> {
    while (this.turns < this.cfg.max_turns) {
      const fold = await this.folder.advance();
      const settled = this.settled(fold);
      if (settled !== null) return settled;

      const refusal = await this.harness.budget.beginCall();
      if (refusal !== null) return this.exhausted(refusal);

      const messages = this.assembled();
      if (this.lastFold > 0) yield { type: "folded", folded: this.lastFold, remaining: messages.length };

      const turn = yield* this.burn({ messages, tools: this.prefix.tools });
      this.turns += 1;
      this.prose = turn.content;
      if (turn.tool_calls.length === 0) return null;

      this.transcript.push({ role: "assistant", content: turn.content, tool_calls: turn.tool_calls });
      for (const call of turn.tool_calls) {
        const tool = this.tools.find((granted) => granted.id === call.tool);
        if (tool === undefined) {
          yield* this.record(call, refused(`${call.tool} is not granted to ${this.cfg.role}`));
          continue;
        }
        const gate = this.gate(tool, call.args, fold);
        if (gate.kind === "park") return await this.park(gate.checkpoint_id, tool.id, call.args);
        if (gate.kind === "rejected") {
          yield* this.record(call, refused("a reviewer rejected this call"));
          continue;
        }
        // Served from the ledger rather than run again: a resume replays the turn
        // from an empty transcript, and a gated call that acts would act twice.
        if (gate.kind === "served") {
          yield* this.record(call, gate.result, gate.checkpoint_id);
          continue;
        }
        yield* this.record(call, await this.invoke(tool, call.args), gate.checkpoint_id);
      }
    }

    // The cap stops the tool loop, not the run: a role that gathered something
    // still answers over what it gathered, and says the set was truncated.
    this.capped = true;
    return null;
  }

  // The store is the authority on whether the run is still going, so one
  // cancelled or answered out of band is seen on the next pass rather than never.
  private settled(fold: Fold): Outcome<T> | null {
    if (fold.terminal !== null && this.cfg.after_terminal !== true) {
      const status: Status = fold.terminal.outcome === "completed" ? "completed" : "failed";
      return this.done(status, null, `the ledger already ended this run: ${fold.terminal.reason}`);
    }
    if (fold.open === null) return null;
    const outcome = this.done("waiting_approval", null, `the ledger holds an open checkpoint, ${fold.open}`);
    return { ...outcome, pending: { checkpoint_id: fold.open, tool: null, args: null } };
  }

  // An approval says a call may go through; the executed record says it already
  // has. Only the second stops a resume making the same call a second time.
  private gate(tool: RegisteredTool, args: string, fold: Fold): Gate {
    if (!this.cfg.approvals.has(tool.id)) return { kind: "allowed" };
    const checkpoint_id = approvalId(this.cfg.run_id, tool.id, args);
    const served = fold.executed.get(checkpoint_id);
    if (served !== undefined) return { kind: "served", checkpoint_id, result: served };
    const answer = fold.answered.get(checkpoint_id);
    if (answer === undefined) return { kind: "park", checkpoint_id };
    return answer === "approve" ? { kind: "allowed", checkpoint_id } : { kind: "rejected" };
  }

  private async invoke(tool: RegisteredTool, rawArgs: string): Promise<ToolResult> {
    const args = parseArgs(rawArgs);
    if (args === null) return { ok: false, failure: { kind: "invalid_args", detail: "arguments were not valid JSON" } };
    return this.harness.dispatch.invoke(tool, args, this.cfg.signal);
  }

  // The one path a result takes, and where wrap scans it. A gated call is journaled
  // with its outcome, so a later attempt is served from the ledger instead of run.
  private async *record(call: ToolCall, result: ToolResult, gated?: string): AsyncGenerator<StreamEvent<T>, void> {
    yield { type: "tool_call", call };
    const wrapped = wrap(call.tool, result, this.scan, this.cfg.result_cap);
    const attempt: Attempt = { tool: call.tool, args: call.args, result, wrapped };
    this.calls.push(attempt);
    this.transcript.push({ role: "tool", call_id: call.id, content: wrapped.text });
    if (gated !== undefined) await this.journalExecuted(gated, call.tool, result);
    yield { type: "tool_result", call, attempt };
  }

  // The dispatch event's own id is the checkpoint's, so the next pass finds it by
  // the same derivation that raised the approval.
  private async journalExecuted(checkpointId: string, tool: string, result: ToolResult): Promise<void> {
    const payload: DispatchPayload = {
      dispatch_id: checkpointId,
      agent_id: this.cfg.role,
      status: result.ok ? "complete" : "failed",
      question_id: null,
      failure_reason: result.ok ? null : result.failure.kind,
      result,
    };
    await this.write({ run_id: this.cfg.run_id, run_kind: this.cfg.run_kind, kind: "dispatch", payload });
  }

  private async *emit(schema: Record<string, unknown>): AsyncGenerator<StreamEvent<T>, Outcome<T>> {
    const validate = compile(schema);
    // The ask and any correction are the transient tail: they belong to this
    // attempt, so they are re-rendered rather than written into the transcript.
    let tail = "Emit your answer now as JSON matching the schema.";

    for (let attempt = 0; attempt < 2; attempt += 1) {
      const refusal = await this.harness.budget.beginCall();
      if (refusal !== null) return this.exhausted(refusal);

      // A write-up that died has still gathered everything behind it, so the only thing
      // worth changing before asking again is how much it is asked over.
      let turn;
      try {
        turn = yield* this.burn({ messages: this.assembled(tail), tools: [], emit: schema });
      } catch (error) {
        const tighter = FOLD_LADDER[this.folds];
        if (tighter === undefined || this.cfg.signal?.aborted === true) throw error;
        this.tightened = tighter;
        this.folds += 1;
        this.rejected.push(`the emission call failed (${(error as Error).message}); asked again over a folded transcript`);
        attempt -= 1;
        continue;
      }

      const parsed = tryParse(turn.content);
      if (parsed !== undefined && validate(parsed)) {
        return this.done("completed", parsed as T, "the role answered");
      }

      const reason = parsed === undefined ? "the response was not valid JSON" : errorsOf(validate);
      this.rejected.push(`${reason}: ${turn.content.slice(0, 400)}`);
      // The rejected emission goes back as the assistant turn it was, or the model
      // is asked to correct something it cannot see.
      tail = [
        "Emit your answer now as JSON matching the schema.",
        turn.content,
        `That emission was rejected -- ${reason}. Emit a valid answer.`,
      ].join("\n\n");
    }

    return this.done("failed", null, `the role never emitted a valid answer: ${this.rejected.join(" | ")}`);
  }

  // Prefix, then the folded history, then a tail that is never persisted. What
  // summarising drops is the fold's to decide, and the edges are never dropped.
  private assembled(working = ""): Message[] {
    const policy = this.tightened ?? undefined;
    const { messages, folded } = assemble(
      this.prefix,
      this.cfg.task,
      this.transcript,
      working,
      summariseFolded,
      ...(policy === undefined ? [] : ([policy] as const)),
    );
    this.lastFold = folded;
    return messages;
  }

  // One model call, journaled as the provider reports it rather than after it
  // returns: a call that dies mid-turn has still put its spend on the ledger.
  private async *burn(request: Omit<TurnRequest, "signal">): AsyncGenerator<StreamEvent<T>, Omit<Turn, "tokens">> {
    const signal = this.cfg.signal ? { signal: this.cfg.signal } : {};
    const tool_calls: ToolCall[] = [];
    let content = "";
    let billed = false;

    try {
      for await (const event of this.harness.provider.stream({ ...request, ...signal })) {
        if (event.type === "tool_call") tool_calls.push(event.call);
        else if (event.type === "text_delta") {
          content += event.text;
          yield event;
        } else {
          billed = true;
          yield { type: "usage", payload: await this.settle(event.tokens) };
        }
      }
    } catch (error) {
      // Only when the provider died without reporting: it carries what it burned
      // precisely so a failure before the usage event is not spend the pool loses.
      if (!billed) await this.settle(error instanceof ProviderError ? error.tokens : ZERO_TOKENS);
      throw error;
    }

    return { content, tool_calls };
  }

  // Priced before recorded, so the spend fold is in dollars and the pool has something
  // to hold. Null when nothing priced it: an unpriced call is not a free one.
  private async settle(tokens: TokenCounts): Promise<SpendPayload> {
    const model_id = this.harness.provider.model;
    const provider_type = this.harness.provider.provider_type;
    const priced = await this.harness.budget.priceOf(model_id, provider_type, tokens);
    const payload: SpendPayload = {
      model_id,
      provider_type,
      role: this.cfg.role,
      tokens,
      cost_usd: priced.cost_usd,
      pricing_source: priced.source,
    };
    this.harness.budget.record(payload);
    this.spent += payload.cost_usd ?? 0;
    await this.write({ run_id: this.cfg.run_id, run_kind: this.cfg.run_kind, kind: "spend", payload });
    return payload;
  }

  private async park(checkpoint_id: string, tool: string, args: string): Promise<Outcome<T>> {
    const payload: CheckpointPayload = {
      checkpoint_id,
      checkpoint_class: TOOL_APPROVAL,
      question: `${this.cfg.role} wants to call ${tool} with ${args}. Approve?`,
      raised_at: new Date().toISOString(),
    };
    await this.write({ run_id: this.cfg.run_id, run_kind: this.cfg.run_kind, kind: "checkpoint", payload });
    const outcome = this.done("waiting_approval", null, `parked on approval for ${tool}`);
    return { ...outcome, pending: { checkpoint_id, tool, args } };
  }

  // Written as it happens, not returned to be written: a workflow cannot discard
  // what is already on the ledger, and a killed process has still recorded it.
  private async write(event: NewEvent<Record<never, never>>): Promise<void> {
    await this.harness.state.append(this.cfg.run_id, [event as unknown as NewEvent<Kinds>]);
  }

  private exhausted(refusal: Refusal): Outcome<T> {
    const reason = `the budget refused another iteration: ${refusal.reason}`;
    return { ...this.done("failed", null, reason), refusal };
  }

  private done(status: Status, value: T | null, reason: string): Outcome<T> {
    return {
      status,
      value,
      refusal: null,
      pending: null,
      capped: this.capped,
      transcript: this.transcript,
      calls: this.calls,
      turns: this.turns,
      rejected: this.rejected,
      reason,
      cost_usd: Number(this.spent.toFixed(6)),
    };
  }
}

// Not every throw is a turn that failed: an exhausted gateway cannot serve the next
// call either, and a defect in this process is not a role that could not answer.
function hardStop(error: unknown): boolean {
  if (error instanceof TypeError || error instanceof ReferenceError || error instanceof SyntaxError) return true;
  return error instanceof GatewayExhausted || error instanceof ToolBoundsViolation;
}

// The last event a run yields and the outcome it returns are the same thing, so
// a caller that reads only the stream and one that awaits it agree.
async function* announce<T>(outcome: Outcome<T>): TurnStream<T> {
  if (outcome.pending !== null) yield { type: "approval_required", pending: outcome.pending };
  else yield { type: outcome.status === "completed" ? "done" : "failed", outcome };
  return outcome;
}

type Gate =
  | { kind: "allowed"; checkpoint_id?: string }
  | { kind: "rejected" }
  | { kind: "park"; checkpoint_id: string }
  | { kind: "served"; checkpoint_id: string; result: ToolResult };

interface Fold {
  answered: ReadonlyMap<string, ResolutionPayload["answer"]>;
  // Gated calls this run has already made, by the checkpoint that approved them.
  executed: ReadonlyMap<string, ToolResult>;
  // A checkpoint with no approving or rejecting resolution, which is what keeps
  // a run parked whether or not this harness is the one that raised it.
  open: string | null;
  terminal: TerminalPayload | null;
}

// The three questions the loop asks of the store each pass, folded once and then
// advanced. Re-reading the whole ledger every tool turn is quadratic in the run.
class Folder<Kinds extends Record<string, unknown>> {
  private readonly answered = new Map<string, ResolutionPayload["answer"]>();
  private readonly executed = new Map<string, ToolResult>();
  private readonly raised: string[] = [];
  private terminal: TerminalPayload | null = null;
  private next = 0;

  constructor(private readonly state: State<Kinds>, private readonly runId: string) {}

  async advance(): Promise<Fold> {
    for (const event of await this.state.read(this.runId, { since: this.next })) {
      this.next = Math.max(this.next, event.seq + 1);
      if (event.kind === "resolution") {
        const payload = event.payload as ResolutionPayload;
        this.answered.set(payload.checkpoint_id, payload.answer);
      }
      if (event.kind === "checkpoint") this.raised.push((event.payload as CheckpointPayload).checkpoint_id);
      if (event.kind === "dispatch") {
        const payload = event.payload as DispatchPayload;
        if (payload.result !== undefined) this.executed.set(payload.dispatch_id, payload.result as ToolResult);
      }
      if (event.kind === "terminal") this.terminal = event.payload as TerminalPayload;
    }

    return {
      answered: this.answered,
      executed: this.executed,
      open: this.raised.find((id) => !this.answered.has(id)) ?? null,
      terminal: this.terminal,
    };
  }
}

// Tried in order, one step per failed write-up. DEFAULT_FOLD already bounds a request
// by weight, so this is the backstop rather than the mechanism: a ceiling lower than
// the one DEFAULT_FOLD was set for, reached only after a write-up has already died.
// Both ceilings are reachable, which matters: neither edge folds below one message, so
// the floor is roughly two result_caps and a budget under that is a budget the fold can
// never meet. It would spend the whole ladder failing to.
const FOLD_LADDER: readonly FoldPolicy[] = [
  { head: 2, tail: 4, max_messages: 10, max_chars: 60_000 },
  { head: 1, tail: 1, max_messages: 4, max_chars: 40_000 },
];

// Names what was dropped rather than reproducing it: a summary that quotes the
// middle back is the middle, and folds nothing.
function summariseFolded(folded: readonly Message[]): string {
  const calls = folded.filter((one) => one.role === "tool").length;
  return `[${folded.length} earlier messages folded away, including ${calls} tool results.]`;
}

function schemaOf(tool: RegisteredTool): ToolSchema {
  return { id: tool.id, description: tool.description, parameters: tool.parameters };
}

function refused(detail: string): ToolResult {
  return { ok: false, failure: { kind: "refused", detail } };
}

function parseArgs(raw: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(raw === "" ? "{}" : raw);
    return typeof parsed === "object" && parsed !== null ? (parsed as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

function tryParse(content: string): unknown {
  try {
    return JSON.parse(fenceless(content));
  } catch {
    return undefined;
  }
}

// Some models return the object inside a markdown code fence, which is a correct
// answer this layer would otherwise reject as unparseable and pay to ask again.
function fenceless(content: string): string {
  const fenced = /^\s*```(?:json)?\s*([\s\S]*?)\s*```\s*$/.exec(content);
  return fenced === null ? content : fenced[1]!;
}

const ajv = new Ajv({ allErrors: true, strict: false });
const compiled = new Map<string, ValidateFunction>();

function compile(schema: Record<string, unknown>): ValidateFunction {
  const key = JSON.stringify(schema);
  const existing = compiled.get(key);
  if (existing !== undefined) return existing;
  const validate = ajv.compile(schema);
  compiled.set(key, validate);
  return validate;
}

function errorsOf(validate: ValidateFunction): string {
  return (validate.errors ?? []).map((error) => `${error.instancePath || "/"} ${error.message ?? ""}`).join("; ");
}
