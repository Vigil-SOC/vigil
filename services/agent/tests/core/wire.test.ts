import { beforeEach, describe, expect, it } from "vitest";
import type OpenAI from "openai";
import { Limiter } from "../../core/limiter.js";
import { EMIT_TOOL, openAiSurface, resetEmitMode, resetOutputCap } from "../../core/wire.js";
import { ZERO_TOKENS, type TokenCounts } from "../../contracts/budget.js";
import type { Message, Provider, ToolCall, Turn, TurnRequest } from "../../core/provider.js";

type Body = OpenAI.Chat.ChatCompletionCreateParams;

type Chunk = OpenAI.Chat.ChatCompletionChunk;

// Split so a reassembly that drops a fragment or keeps only the last one fails
// here rather than in a run.
function halves(text: string): string[] {
  if (text.length < 2) return [text];
  const at = Math.floor(text.length / 2);
  return [text.slice(0, at), text.slice(at)];
}

const SCHEMA = { type: "object", required: ["verb"], properties: { verb: { type: "string" } } };

function limiter(): Limiter {
  return new Limiter({ rpm: 10_000, tpm: 10_000_000 }, 4, 1);
}

// A whole message, delivered the way a gateway delivers one: content in pieces, a
// tool call opened by a fragment carrying id and name and continued by fragments
// carrying null for both, and usage alone in a final chunk with no choice at all.
// Authored as the finished message because that is what each test is about.
function completion(
  message: Record<string, unknown>,
  usage?: Record<string, unknown> | null,
): AsyncIterable<Chunk> {
  const chunks: Chunk[] = [];
  const push = (delta: Record<string, unknown>) =>
    chunks.push({
      id: "chunk-1",
      created: 1,
      model: "m",
      object: "chat.completion.chunk",
      choices: [{ index: 0, delta, finish_reason: null }],
    } as unknown as Chunk);

  const content = message["content"];
  if (typeof content === "string") {
    for (const piece of halves(content)) push({ content: piece });
  } else if (content !== undefined && content !== null) {
    // A content-block list, which textOf flattens; handed over whole.
    push({ content });
  }

  const calls = (message["tool_calls"] ?? []) as { id: string; function: { name: string; arguments: string } }[];
  for (const [index, call] of calls.entries()) {
    push({ tool_calls: [{ index, type: "function", id: call.id, function: { name: call.function.name, arguments: "" } }] });
    for (const piece of halves(call.function.arguments ?? "")) {
      push({ tool_calls: [{ index, type: "function", function: { name: null, arguments: piece } }] });
    }
  }

  if (usage !== null) {
    chunks.push({
      id: "chunk-1",
      created: 1,
      model: "m",
      object: "chat.completion.chunk",
      choices: [],
      usage: usage ?? { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 },
    } as unknown as Chunk);
  }

  return {
    async *[Symbol.asyncIterator]() {
      for (const chunk of chunks) yield chunk;
    },
  };
}

function surfaceOf(create: (body: Body) => Promise<AsyncIterable<Chunk>>, model = "openai/gpt-4o") {
  const client = { chat: { completions: { create } } } as unknown as OpenAI;
  return openAiSurface(client, model, limiter(), "openai");
}

// The surface streams; what these assertions are about is the assembled call.
async function turn(surface: Provider, request: TurnRequest): Promise<Turn> {
  const tool_calls: ToolCall[] = [];
  let content = "";
  let tokens = ZERO_TOKENS;
  for await (const event of surface.stream(request)) {
    if (event.type === "text_delta") content += event.text;
    else if (event.type === "usage") tokens = event.tokens;
    else tool_calls.push(event.call);
  }
  return { content, tool_calls, tokens };
}

beforeEach(() => {
  resetEmitMode();
  resetOutputCap();
});

describe("the OpenAI surface", () => {
  it("makes one call and hands back what the model said", async () => {
    const bodies: Body[] = [];
    const surface = surfaceOf(async (body) => {
      bodies.push(body);
      return completion({ role: "assistant", content: "thinking about it" });
    });

    const first = await turn(surface, { messages: [{ role: "user", content: "go" }], tools: [] });
    expect(first.content).toBe("thinking about it");
    expect(first.tool_calls).toEqual([]);
    expect(bodies).toHaveLength(1);
    expect(bodies[0]!.max_tokens).toBeGreaterThan(4096);
    // No tools offered means the key is absent, not present and empty: some
    // providers reject an empty tools array outright.
    expect(bodies[0]!.tools).toBeUndefined();
  });

  // A gateway holds a ceiling on any call carrying a tools array that no provider
  // setting moves, so the wire that cannot be used is the forced tool -- and the
  // schema has to reach the model some other way or the write-up is simply lost.
  it("asks for the emission in the prompt when the gateway refuses a call carrying tools", async () => {
    const bodies: Body[] = [];
    const surface = surfaceOf(async (body) => {
      bodies.push(body);
      if (body.response_format !== undefined) return completion({ role: "assistant", content: '{"invented":1}' });
      if (body.tools !== undefined) throw Object.assign(new Error("tools are not supported here"), { status: 400 });
      return completion({ role: "assistant", content: '{"verb":"HALT"}' });
    });

    const emitted = await turn(surface, {
      messages: [{ role: "user", content: "go" }],
      tools: [],
      emit: SCHEMA,
    });

    expect(emitted.content).toBe('{"verb":"HALT"}');
    const last = bodies.at(-1)!;
    expect(last.tools).toBeUndefined();
    expect(last.response_format).toBeUndefined();
    // The schema itself is what reaches the model, not a description of one.
    expect(JSON.stringify(last.messages)).toContain("verb");
  });

  // A 504 is the gateway saying "not now", and reading it as "not ever" downgraded
  // every role in the process to the rung that enforces no schema at all -- for good,
  // off one slow call. The forced tool has to be tried again on the next emission.
  it("does not give up on the forced tool because one call hit the gateway's ceiling", async () => {
    const bodies: Body[] = [];
    let ceiling = true;
    const surface = surfaceOf(async (body) => {
      bodies.push(body);
      if (body.response_format !== undefined) return completion({ role: "assistant", content: '{"invented":1}' });
      if (ceiling) {
        ceiling = false;
        throw Object.assign(new Error("request timed out"), { status: 504 });
      }
      return completion({
        role: "assistant",
        tool_calls: [{ id: "1", function: { name: EMIT_TOOL, arguments: '{"verb":"HALT"}' } }],
      });
    });

    const request: TurnRequest = { messages: [{ role: "user", content: "go" }], tools: [], emit: SCHEMA };
    // The ceiling escapes rather than being swallowed as "this wire does not work".
    await expect(turn(surface, request)).rejects.toThrow("request timed out");

    const emitted = await turn(surface, request);
    expect(emitted.content).toBe('{"verb":"HALT"}');
    // Asked for by name, so the wire was not remembered as unusable.
    expect(bodies.at(-1)!.tool_choice).toEqual({ type: "function", function: { name: EMIT_TOOL } });
  });

  // The reasoning families 400 on max_tokens and take max_completion_tokens. The tool
  // loop has no 400 handler, so every turn failed on a field the caller never chose.
  it("moves the output ceiling to the field the model takes it under, once", async () => {
    const bodies: Body[] = [];
    const surface = surfaceOf(async (body) => {
      bodies.push(body);
      if (body.max_tokens !== undefined) {
        throw Object.assign(new Error("Unsupported parameter: 'max_tokens' is not supported with this model."), {
          status: 400,
        });
      }
      return completion({ role: "assistant", content: "done" });
    }, "openai/gpt-5");

    expect((await turn(surface, { messages: [{ role: "user", content: "go" }], tools: [] })).content).toBe("done");
    expect(bodies).toHaveLength(2);
    expect(bodies[1]!.max_completion_tokens).toBeGreaterThan(4096);

    // Remembered, so the second turn does not pay to rediscover it.
    await turn(surface, { messages: [{ role: "user", content: "again" }], tools: [] });
    expect(bodies).toHaveLength(3);
    expect(bodies[2]!.max_tokens).toBeUndefined();
  });

  // Measured: with no tools array in the request at all, a provider that finds a name in
  // the transcript history calls it, finishes on tool_calls, and writes no content. The
  // forced wire already corrected this; the prompted one returned the empty string,
  // which the caller reads as unparseable and spends both its retries on -- so a role
  // that gathered eight results reported having gathered none.
  it("names the function a prompted emission reached for instead of answering", async () => {
    const bodies: Body[] = [];
    let reached = true;
    const surface = surfaceOf(async (body) => {
      bodies.push(body);
      if (body.response_format !== undefined) return completion({ role: "assistant", content: '{"invented":1}' });
      if (body.tools !== undefined) throw Object.assign(new Error("no tools here"), { status: 400 });
      if (reached) {
        reached = false;
        return completion({
          role: "assistant",
          tool_calls: [{ id: "1", function: { name: "splunk_execute", arguments: "{}" } }],
        });
      }
      return completion({ role: "assistant", content: '{"verb":"HALT"}' });
    });

    const emitted = await turn(surface, { messages: [{ role: "user", content: "go" }], tools: [], emit: SCHEMA });

    expect(emitted.content).toBe('{"verb":"HALT"}');
    const last = bodies.at(-1)!;
    expect(last.tools).toBeUndefined();
    // Named, so the model can see what it did rather than only that it was wrong.
    expect(JSON.stringify(last.messages)).toContain("splunk_execute");
    expect(JSON.stringify(last.messages)).toContain("not available here");
  });

  it("remembers that a deployment carries neither wire, so a second emission asks once", async () => {
    const bodies: Body[] = [];
    const surface = surfaceOf(async (body) => {
      bodies.push(body);
      if (body.response_format !== undefined) return completion({ role: "assistant", content: '{"invented":1}' });
      if (body.tools !== undefined) throw Object.assign(new Error("tools are not supported here"), { status: 400 });
      return completion({ role: "assistant", content: '{"verb":"HALT"}' });
    });

    const request: TurnRequest = { messages: [{ role: "user", content: "go" }], tools: [], emit: SCHEMA };
    await turn(surface, request);
    const before = bodies.length;
    await turn(surface, request);

    // One call, not three: the two wires that do not work here are not tried again.
    expect(bodies.length - before).toBe(1);
    expect(bodies.at(-1)!.tools).toBeUndefined();
  });

  // The guard that matters: a fault in this process must not be mistaken for a
  // deployment that cannot carry a tool, or a bug becomes a silent mode change.
  it("lets an error with no status escape rather than downgrading the wire", async () => {
    const surface = surfaceOf(async (body) => {
      if (body.response_format !== undefined) return completion({ role: "assistant", content: '{"invented":1}' });
      throw new TypeError("someUndefined is not a function");
    });

    await expect(
      turn(surface, { messages: [{ role: "user", content: "go" }], tools: [], emit: SCHEMA }),
    ).rejects.toThrow(/not a function/);
  });

  it("reports tool calls the model asked for", async () => {
    const surface = surfaceOf(async () =>
      completion({
        role: "assistant",
        content: null,
        tool_calls: [{ id: "c1", type: "function", function: { name: "bump", arguments: '{"by":1}' } }],
      }),
    );

    const asked = await turn(surface, {
      messages: [{ role: "user", content: "go" }],
      tools: [{ id: "bump", description: "increment", parameters: {} }],
    });
    expect(asked.tool_calls).toEqual([{ id: "c1", tool: "bump", args: '{"by":1}' }]);
  });

  // Handed to JSON.parse a block list stringifies to [object Object], so an
  // answer the model got right would be discarded as invalid JSON.
  it("flattens a reply that arrives as content blocks", async () => {
    const surface = surfaceOf(async () =>
      completion({ role: "assistant", content: [{ type: "text", text: '{"verb":' }, { type: "text", text: '"HALT"}' }] }),
    );
    const blocks = await turn(surface, { messages: [{ role: "user", content: "go" }], tools: [], emit: SCHEMA });
    expect(blocks.content).toBe('{"verb":"HALT"}');
  });

  it("carries the transcript back to the wire, tool turns included", async () => {
    const bodies: Body[] = [];
    const surface = surfaceOf(async (body) => {
      bodies.push(body);
      return completion({ role: "assistant", content: "done" });
    });

    const messages: Message[] = [
      { role: "system", content: "be brief" },
      { role: "user", content: "go" },
      { role: "assistant", content: "", tool_calls: [{ id: "c1", tool: "bump", args: "{}" }] },
      { role: "tool", call_id: "c1", content: "1 row" },
    ];
    await turn(surface, { messages, tools: [] });

    const sent = bodies[0]!.messages;
    expect(sent.map((message) => message.role)).toEqual(["system", "user", "assistant", "tool"]);
    expect(sent[2]).toEqual({
      role: "assistant",
      content: "",
      tool_calls: [{ id: "c1", type: "function", function: { name: "bump", arguments: "{}" } }],
    });
    expect(sent[3]).toEqual({ role: "tool", tool_call_id: "c1", content: "1 row" });
  });
});

describe("the emission turn", () => {
  it("asks for a schema-constrained answer and offers no tools", async () => {
    const bodies: Body[] = [];
    const surface = surfaceOf(async (body) => {
      bodies.push(body);
      return completion({ role: "assistant", content: '{"verb":"HALT"}' });
    });

    await turn(surface, {
      messages: [{ role: "user", content: "go" }],
      tools: [{ id: "bump", description: "increment", parameters: {} }],
      emit: SCHEMA,
    });
    expect(bodies[0]!.response_format).toBeDefined();
    expect(bodies[0]!.tools).toBeUndefined();
  });

  it("downgrades to a tool-shaped emit when the gateway rejects the format, once", async () => {
    const bodies: Body[] = [];
    const surface = surfaceOf(async (body) => {
      bodies.push(body);
      if (body.response_format !== undefined) throw Object.assign(new Error("unsupported"), { status: 400 });
      return completion({
        role: "assistant",
        tool_calls: [{ id: "e1", type: "function", function: { name: EMIT_TOOL, arguments: '{"verb":"HALT"}' } }],
      });
    });

    const request = { messages: [{ role: "user" as const, content: "go" }], tools: [], emit: SCHEMA };
    // The arguments come back as content, so the loop validates one shape
    // whichever mode produced it.
    expect((await turn(surface, request)).content).toBe('{"verb":"HALT"}');
    expect((await turn(surface, request)).tool_calls).toEqual([]);

    // Remembered, so the second emission never probes response_format again.
    expect(bodies.filter((body) => body.response_format !== undefined)).toHaveLength(1);
    expect(bodies.at(-1)!.tool_choice).toEqual({ type: "function", function: { name: EMIT_TOOL } });
  });

  // Remembered per model: response_format is a property of the provider behind
  // one model name, so one gateway's 400 must not downgrade every other model.
  it("does not downgrade a different model on another's rejection", async () => {
    const bodies: Body[] = [];
    const reject = async (body: Body) => {
      bodies.push(body);
      if (body.response_format !== undefined) throw Object.assign(new Error("unsupported"), { status: 400 });
      return completion({
        role: "assistant",
        tool_calls: [{ id: "e1", type: "function", function: { name: EMIT_TOOL, arguments: "{}" } }],
      });
    };

    await turn(surfaceOf(reject, "legacy/model"), { messages: [], tools: [], emit: SCHEMA });
    bodies.length = 0;
    await turn(surfaceOf(reject, "openai/gpt-4o"), { messages: [], tools: [], emit: SCHEMA });
    expect(bodies[0]!.response_format).toBeDefined();
  });

  // Keyed by schema as well: Anthropic refuses a schema carrying a free-form
  // object, which is the worker's, and accepts the lead's. Keyed by model alone
  // the worker's 400 downgraded the lead, whose schema the gateway takes.
  it("does not downgrade another schema on one schema's rejection", async () => {
    const refused = { type: "object", properties: { payload: { type: "object" } } };
    const bodies: Body[] = [];
    const surface = surfaceOf(async (body) => {
      bodies.push(body);
      const schema = body.response_format?.type === "json_schema" ? body.response_format.json_schema.schema : undefined;
      if (schema === refused) throw Object.assign(new Error("additionalProperties"), { status: 400 });
      return completion({
        role: "assistant",
        content: "{}",
        tool_calls: [{ id: "e1", type: "function", function: { name: EMIT_TOOL, arguments: "{}" } }],
      });
    });

    await turn(surface, { messages: [], tools: [], emit: refused });
    bodies.length = 0;
    await turn(surface, { messages: [], tools: [], emit: SCHEMA });
    expect(bodies[0]!.response_format).toBeDefined();
  });

  it("does not read a non-400 failure as a reason to downgrade", async () => {
    const surface = surfaceOf(async () => {
      throw Object.assign(new Error("unauthorized"), { status: 401 });
    });
    await expect(turn(surface, { messages: [], tools: [], emit: SCHEMA })).rejects.toThrow(/unauthorized/);
  });
});

describe("token accounting", () => {
  it("reads the cached share from whichever surface reported it", async () => {
    const openai = await usage({
      prompt_tokens: 100,
      completion_tokens: 20,
      prompt_tokens_details: { cached_tokens: 60 },
    });
    expect(openai).toEqual({ input: 100, output: 20, cache_read: 60, cache_write: 0 });

    const alternate = await usage({
      prompt_tokens: 40,
      completion_tokens: 8,
      cache_read_input_tokens: 10,
      cache_creation_input_tokens: 30,
    });
    // input is the total, so Anthropic's two cache counters are added back;
    // natively input_tokens excludes both and the call would price two ways.
    expect(alternate).toEqual({ input: 80, output: 8, cache_read: 10, cache_write: 30 });
  });

  // The OpenAI route already counts the cached share inside prompt_tokens, so
  // adding it back there would bill it twice.
  it("does not double-count a cached share the OpenAI route already included", async () => {
    const tokens = await usage({ prompt_tokens: 100, completion_tokens: 20, prompt_tokens_details: { cached_tokens: 90 } });
    expect(tokens.input).toBe(100);
    expect(tokens.cache_read).toBe(90);
  });

  // Zeroes read as "this call was free", not as "nobody counted": the rate table
  // prices four zeros at exactly $0.00 with a real source, so the pool's cost never
  // grew and no ceiling ever bound. A gateway that drops stream_options sends no usage
  // chunk at all, and a run that cannot see its own spend runs to the iteration cap.
  it("estimates the tokens when the stream sends no usage, rather than recording a free call", async () => {
    const surface = surfaceOf(async () => completion({ role: "assistant", content: "ok" }, null));
    const tokens = (await turn(surface, { messages: [{ role: "user", content: "a long enough question" }], tools: [] })).tokens;
    expect(tokens.input).toBeGreaterThan(0);
    expect(tokens.output).toBeGreaterThan(0);
    expect(tokens.cache_read).toBe(0);
    expect(tokens.cache_write).toBe(0);
  });
});

// The wire was buffered while the method was called stream() and the interface
// said "assembled from its stream". A buffered completion is subject to the
// gateway's non-streaming ceiling -- 30 seconds in Bifrost, unmovable by any
// network_config value and returned as a 504 -- so every call that ran longer
// died, and a worker writing a long answer died while the lead's short decisions
// went through.
describe("the wire streams", () => {
  it("asks the gateway for a stream, and for usage with it", async () => {
    const bodies: Body[] = [];
    const surface = surfaceOf(async (body) => {
      bodies.push(body);
      return completion({ role: "assistant", content: "ok" });
    });

    await turn(surface, { messages: [{ role: "user", content: "hi" }], tools: [] });

    expect(bodies[0]?.stream).toBe(true);
    // Without it the final chunk carries no usage and every call would be free.
    expect(bodies[0]?.stream_options).toEqual({ include_usage: true });
  });

  it("keeps every fragment of a reply rather than the last one", async () => {
    const surface = surfaceOf(async () =>
      completion({ role: "assistant", content: "the whole answer, in pieces" }),
    );

    expect((await turn(surface, { messages: [], tools: [] })).content).toBe("the whole answer, in pieces");
  });

  // The fragments after the first carry null for id and name, so an accumulator
  // that overwrites rather than keeps loses the name and the call is unroutable.
  it("rebuilds a tool call split across fragments", async () => {
    const surface = surfaceOf(async () =>
      completion({
        role: "assistant",
        content: "",
        tool_calls: [{ id: "call-1", type: "function", function: { name: "splunk_execute", arguments: '{"spl_query":"index=botsv3"}' } }],
      }),
    );

    const [call] = (await turn(surface, { messages: [], tools: [] })).tool_calls;
    expect(call?.id).toBe("call-1");
    expect(call?.tool).toBe("splunk_execute");
    expect(JSON.parse(call?.args ?? "{}")).toEqual({ spl_query: "index=botsv3" });
  });

  it("keeps two tool calls apart and in the order the model asked for them", async () => {
    const surface = surfaceOf(async () =>
      completion({
        role: "assistant",
        content: "",
        tool_calls: [
          { id: "call-1", type: "function", function: { name: "first", arguments: '{"a":1}' } },
          { id: "call-2", type: "function", function: { name: "second", arguments: '{"b":2}' } },
        ],
      }),
    );

    const { tool_calls } = await turn(surface, { messages: [], tools: [] });
    expect(tool_calls.map((one) => one.tool)).toEqual(["first", "second"]);
    expect(tool_calls.map((one) => one.args)).toEqual(['{"a":1}', '{"b":2}']);
  });

  it("refuses a gateway that answers a stream request with a whole completion", async () => {
    const surface = surfaceOf(
      async () => ({ choices: [{ message: { role: "assistant", content: "ok" } }] }) as unknown as AsyncIterable<Chunk>,
    );

    await expect(turn(surface, { messages: [], tools: [] })).rejects.toThrow(/whole completion/);
  });

  it("says so when the stream carries nothing at all", async () => {
    const surface = surfaceOf(async () => ({ async *[Symbol.asyncIterator]() {} }));

    await expect(turn(surface, { messages: [], tools: [] })).rejects.toThrow(/without sending anything/);
  });
});

async function usage(reported: Record<string, unknown>): Promise<TokenCounts> {
  const surface = surfaceOf(async () => completion({ role: "assistant", content: "ok" }, reported));
  return (await turn(surface, { messages: [], tools: [] })).tokens;
}
