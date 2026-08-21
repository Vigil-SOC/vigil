import OpenAI from "openai";
import type { TokenCounts } from "../contracts/budget.js";
import { estimateTokens, Limiter, statusOf } from "./limiter.js";
import {
  ProviderError,
  type Message,
  type Provider,
  type ProviderEvent,
  type ToolCall,
  type ToolSchema,
  type Turn,
  type TurnRequest,
} from "./provider.js";

// Unset, the gateway's own default cuts a long emission off mid-JSON, which
// arrives as an unparseable answer rather than as a limit that was hit.
const MAX_OUTPUT_TOKENS = 12_000;

export const EMIT_TOOL = "emit";

type Body = Omit<OpenAI.Chat.ChatCompletionCreateParams, "stream" | "stream_options">;

// Not every provider honours response_format, so a 400 downgrades once to a tool
// whose parameters are the schema. Keyed by model and schema together, because the
// refusal is a property of the schema as much as of the provider.
const emitModes = new Map<string, "schema" | "tool" | "prompt">();

// Statuses that mean the gateway will not carry this shape, however often it is asked.
// Everything else -- a 504, a 429, a dropped socket -- is a fact about load, not shape.
const REFUSED = new Set([400, 404, 422, 501]);

// Which field a model takes its output ceiling under: the reasoning families answer 400
// to max_tokens. Remembered per model, for the reason emitModes is.
type OutputCap = "max_tokens" | "max_completion_tokens";
const outputCaps = new Map<string, OutputCap>();

export function resetOutputCap(): void {
  outputCaps.clear();
}

// The gateway relays the upstream complaint, and it names the field. Matching on that
// rather than on a model-id table, which goes stale on the next model release.
function renamed(error: unknown, current: OutputCap): OutputCap | null {
  if (statusOf(error) !== 400) return null;
  const message = error instanceof Error ? error.message : String(error);
  if (!message.includes(current)) return null;
  return current === "max_tokens" ? "max_completion_tokens" : "max_tokens";
}

export function resetEmitMode(): void {
  emitModes.clear();
}

export function openAiSurface(client: OpenAI, model: string, limiter: Limiter, provider_type: string): Provider {
  return new OpenAiSurface(client, model, limiter, provider_type);
}

// The one surface built. The gateway routes to either provider family behind a
// model name, so a second wire buys nothing until cache_control and thinking.
class OpenAiSurface implements Provider {
  constructor(
    private readonly client: OpenAI,
    readonly model: string,
    private readonly limiter: Limiter,
    readonly provider_type: string,
  ) {}

  // Assembled before the events are emitted, so usage precedes the tool calls. The
  // deltas are not re-emitted: nothing downstream renders a partial turn.
  async *stream(request: TurnRequest): AsyncGenerator<ProviderEvent> {
    const turn = request.emit === undefined ? await this.ask(request) : await this.emit(request, request.emit);
    if (turn.content !== "") yield { type: "text_delta", text: turn.content };
    yield { type: "usage", tokens: turn.tokens };
    for (const call of turn.tool_calls) yield { type: "tool_call", call };
  }

  private async ask(request: TurnRequest): Promise<Turn> {
    const tools = request.tools.length === 0 ? {} : { tools: wireTools(request.tools) };
    return turnOf(await this.call({ model: this.model, messages: wire(request.messages), ...tools }, request.signal));
  }

  private async emit(request: TurnRequest, schema: Record<string, unknown>): Promise<Turn> {
    const messages = wire(request.messages);
    const mode = `${this.model}\n${JSON.stringify(schema)}`;
    if ((emitModes.get(mode) ?? "schema") === "schema") {
      try {
        const format = { type: "json_schema" as const, json_schema: { name: "emission", strict: false, schema } };
        const turn = turnOf(await this.call({ model: this.model, messages, response_format: format }, request.signal));
        // A gateway that drops response_format answers 200 with an object of the model's
        // own invention, so fall through to the tool, whose parameters it does forward.
        if (honours(turn.content, schema)) return turn;
        emitModes.set(mode, "tool");
      } catch (error) {
        if (statusOf(error) !== 400) throw error;
        emitModes.set(mode, "tool");
      }
    }

    if (emitModes.get(mode) !== "prompt") {
      const carried = await this.viaTool(messages, schema, request.signal);
      if (carried !== null) return carried;
      emitModes.set(mode, "prompt");
    }

    // Neither wire carried it, so the schema goes in the prompt, which asks nothing of
    // the gateway. What comes back is JSON the caller already parses and corrects.
    const asked = [...messages, { role: "user" as const, content: prompted(schema) }];
    const turn = turnOf(await this.call({ model: this.model, messages: asked }, request.signal));
    if (turn.content !== "" || turn.tool_calls.length === 0) return turn;

    // No tools were offered and it called one anyway: a provider that finds a name in
    // the transcript reaches for it rather than answering. Correct it, as viaTool does.
    return turnOf(
      await this.call(
        { model: this.model, messages: [...asked, { role: "user", content: instead(turn) }] },
        request.signal,
      ),
    );
  }

  // The emission carried by a forced tool call, or null when this wire cannot carry it
  // here -- the gateway refused, or the model answered with something else.
  private async viaTool(
    messages: OpenAI.Chat.ChatCompletionMessageParam[],
    schema: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<Turn | null> {
    const emit = { name: EMIT_TOOL, description: "Emit your answer.", parameters: schema };
    const forced = {
      model: this.model,
      messages,
      tools: [{ type: "function" as const, function: emit }],
      tool_choice: { type: "function" as const, function: { name: EMIT_TOOL } },
    };

    let turn: Turn;
    try {
      turn = turnOf(await this.call(forced, signal));
    } catch (error) {
      // Only a refusal: returning null claims this wire never works here, which a local
      // fault (no status) and a ceiling ("not now") do not establish.
      if (signal?.aborted === true || !REFUSED.has(statusOf(error) ?? 0)) throw error;
      return null;
    }

    // The emission arrived as the tool's arguments. It is returned as content so
    // the loop validates one shape whichever mode produced it.
    const emitted = emissionOf(turn);
    if (emitted !== undefined) return { ...turn, content: emitted, tool_calls: [] };

    // Asked for the emission and handed a call to something else: a provider that does
    // not enforce tool_choice reaches for a name out of the transcript. Say so plainly.
    const corrected = turnOf(
      await this.call(
        { ...forced, messages: [...messages, { role: "user", content: instead(turn, `Call ${EMIT_TOOL} with your final answer, and call nothing else.`) }] },
        signal,
      ),
    );
    const second = emissionOf(corrected);
    return second === undefined ? null : { ...corrected, content: second, tool_calls: [] };
  }

  // Streamed, and not for the deltas: a buffered completion is subject to the gateway's
  // non-streaming ceiling -- 30 seconds in Bifrost, returned as a 504.
  private async call(body: Body, signal?: AbortSignal): Promise<OpenAI.Chat.ChatCompletion> {
    // Before the limiter, not only inside the request: a call still queued behind
    // a rate limit is the cheapest one to give up on.
    signal?.throwIfAborted();
    const cap = outputCaps.get(this.model) ?? "max_tokens";
    try {
      return await this.send(body, cap, signal);
    } catch (error) {
      const other = renamed(error, cap);
      if (other === null) throw error;
      outputCaps.set(this.model, other);
      return await this.send(body, other, signal);
    }
  }

  private async send(body: Body, cap: OutputCap, signal?: AbortSignal): Promise<OpenAI.Chat.ChatCompletion> {
    const limit = cap === "max_tokens" ? { max_tokens: MAX_OUTPUT_TOKENS } : { max_completion_tokens: MAX_OUTPUT_TOKENS };
    const estimate = estimateTokens(JSON.stringify(body));
    // Assembled inside run() rather than after it, so the rate-limit slot is held
    // for the whole call and a mid-stream failure is retried like any other.
    return this.limiter.run(estimate, async () => {
      const stream = await this.client.chat.completions.create(
        { ...limit, ...body, stream: true, stream_options: { include_usage: true } },
        signal ? { signal } : {},
      );
      if (!(Symbol.asyncIterator in stream)) {
        throw new ProviderError("the gateway answered a stream request with a whole completion");
      }
      const response = await assemble(stream);
      return response.usage === undefined ? { ...response, usage: guessed(estimate, response) } : response;
    });
  }
}

// The schema as an instruction, for a gateway carrying neither a response format nor a
// tool. Indented, and explicit about prose, because a fenced answer costs a parse.
function prompted(schema: Record<string, unknown>): string {
  return [
    "Emit your answer now as JSON matching this schema.",
    "Reply with the JSON object only -- no prose, no code fence.",
    "",
    JSON.stringify(schema, null, 1),
  ].join("\n");
}

// What to say to a model that answered with a call instead of an answer. Naming the
// function it reached for is what makes the correction answerable.
function instead(turn: Turn, ask = "Answer with the JSON object only, and call nothing."): string {
  const called = turn.tool_calls.map((call) => call.tool).join(", ");
  return `You called ${called === "" ? "no function" : called}, which is not available here. ${ask}`;
}

// The forced call's arguments, or undefined when the model called something else.
function emissionOf(turn: Turn): string | undefined {
  return turn.tool_calls.find((call) => call.tool === EMIT_TOOL)?.args;
}

// Whether an emission looks like the schema's rather than the model's own. Shallow on
// purpose: this only decides which wire to use, and the caller validates properly.
function honours(content: string, schema: Record<string, unknown>): boolean {
  const required = schema["required"];
  if (!Array.isArray(required) || required.length === 0) return true;
  let parsed: unknown;
  try {
    parsed = JSON.parse(content);
  } catch {
    return false;
  }
  if (typeof parsed !== "object" || parsed === null) return false;
  return required.every((key) => typeof key === "string" && key in (parsed as Record<string, unknown>));
}

// One completion out of its chunks. A tool call arrives split across them, so held ids
// and names are kept rather than overwritten by the nulls that follow.
async function assemble(stream: AsyncIterable<OpenAI.Chat.ChatCompletionChunk>): Promise<OpenAI.Chat.ChatCompletion> {
  const calls = new Map<number, OpenAI.Chat.ChatCompletionMessageToolCall>();
  let content = "";
  let usage: OpenAI.CompletionUsage | undefined;
  let finish: OpenAI.Chat.ChatCompletion.Choice["finish_reason"] = "stop";
  let head: OpenAI.Chat.ChatCompletionChunk | undefined;

  for await (const chunk of stream) {
    head ??= chunk;
    // Sent once, in a final chunk of its own that carries no choice at all.
    if (chunk.usage) usage = chunk.usage;
    const choice = chunk.choices[0];
    if (choice === undefined) continue;
    if (choice.finish_reason) finish = choice.finish_reason;
    content += textOf(choice.delta.content);
    for (const delta of choice.delta.tool_calls ?? []) {
      const held = calls.get(delta.index);
      calls.set(delta.index, {
        id: delta.id ?? held?.id ?? "",
        type: "function",
        function: {
          name: delta.function?.name ?? held?.function.name ?? "",
          arguments: (held?.function.arguments ?? "") + (delta.function?.arguments ?? ""),
        },
      });
    }
  }

  if (head === undefined) throw new ProviderError("the gateway closed the stream without sending anything");
  const tool_calls = [...calls.entries()].sort(([a], [b]) => a - b).map(([, call]) => call);
  return {
    id: head.id,
    created: head.created,
    model: head.model,
    object: "chat.completion",
    choices: [
      {
        index: 0,
        finish_reason: finish,
        logprobs: null,
        message: { role: "assistant", content, refusal: null, ...(tool_calls.length === 0 ? {} : { tool_calls }) },
      },
    ],
    ...(usage === undefined ? {} : { usage }),
  };
}

// A gateway that does not honour stream_options sends no usage chunk, and zero tokens
// price at $0 rather than null, so nothing would bind the run. An estimate is wrong by
// some margin; $0 is wrong by the whole amount and looks deliberate.
function guessed(promptTokens: number, response: OpenAI.Chat.ChatCompletion): OpenAI.CompletionUsage {
  const message = response.choices[0]?.message;
  const written = (message?.content ?? "") + (message?.tool_calls ?? []).map((call) => call.function.arguments).join("");
  const completion_tokens = estimateTokens(written);
  return { prompt_tokens: promptTokens, completion_tokens, total_tokens: promptTokens + completion_tokens };
}

function turnOf(response: OpenAI.Chat.ChatCompletion): Turn {
  const tokens = tokensOf(response.usage);
  const message = response.choices[0]?.message;
  if (message === undefined) throw new ProviderError("the model returned no message", tokens);
  return { content: textOf(message.content), tool_calls: callsOf(message.tool_calls), tokens };
}

function callsOf(calls: OpenAI.Chat.ChatCompletionMessageToolCall[] | undefined): ToolCall[] {
  return (calls ?? []).flatMap((call) =>
    call.type === "function" ? [{ id: call.id, tool: call.function.name, args: call.function.arguments }] : [],
  );
}

// Some providers reply with a content-block list. Handing an array to JSON.parse
// stringifies it to [object Object], throwing away an answer the model got right.
function textOf(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((block) => (typeof block === "object" && block !== null ? String((block as { text?: unknown }).text ?? "") : ""))
    .join("");
}

// Two surfaces disagreeing about an input token, normalised so input is always the
// total: OpenAI already counts the cached share, Anthropic excludes both counters.
function tokensOf(usage: OpenAI.CompletionUsage | undefined): TokenCounts {
  const alternate = usage as
    | (typeof usage & { cache_read_input_tokens?: number; cache_creation_input_tokens?: number })
    | undefined;
  const reported = usage?.prompt_tokens ?? 0;
  const native = alternate?.cache_read_input_tokens !== undefined || alternate?.cache_creation_input_tokens !== undefined;
  const cache_read = usage?.prompt_tokens_details?.cached_tokens ?? alternate?.cache_read_input_tokens ?? 0;
  const cache_write = alternate?.cache_creation_input_tokens ?? 0;
  return {
    input: native ? reported + cache_read + cache_write : reported,
    output: usage?.completion_tokens ?? 0,
    cache_read,
    cache_write,
  };
}

function wire(messages: readonly Message[]): OpenAI.Chat.ChatCompletionMessageParam[] {
  return messages.map((message) => {
    if (message.role === "tool") return { role: "tool", tool_call_id: message.call_id, content: message.content };
    if (message.role !== "assistant") return { role: message.role, content: message.content };
    if (message.tool_calls.length === 0) return { role: "assistant", content: message.content };
    return { role: "assistant", content: message.content, tool_calls: message.tool_calls.map(wireCall) };
  });
}

function wireCall(call: ToolCall): OpenAI.Chat.ChatCompletionMessageToolCall {
  return { id: call.id, type: "function", function: { name: call.tool, arguments: call.args } };
}

function wireTools(tools: readonly ToolSchema[]): OpenAI.Chat.ChatCompletionTool[] {
  return tools.map((tool) => ({
    type: "function",
    function: { name: tool.id, description: tool.description, parameters: tool.parameters },
  }));
}
