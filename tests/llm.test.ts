import { beforeEach, describe, expect, it } from "vitest";
import type OpenAI from "openai";
import { EMIT_TOOL, LlmError, llm_output, renderDispatch, resetEmitMode } from "../ai/llm.js";
import { Limiter } from "../ai/limiter.js";
import type { DispatchRequest } from "../ai/types.js";

type Body = OpenAI.Chat.ChatCompletionCreateParamsNonStreaming;

const SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["action"],
  properties: { action: { type: "string", enum: ["CONCLUDE"] } },
};

function limiter(): Limiter {
  return new Limiter({ rpm: 10_000, tpm: 10_000_000 }, 4, 1);
}

function completion(message: Record<string, unknown>): OpenAI.Chat.ChatCompletion {
  return {
    choices: [{ message, finish_reason: "stop", index: 0, logprobs: null }],
    usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 },
  } as unknown as OpenAI.Chat.ChatCompletion;
}

function clientOf(create: (body: Body) => Promise<OpenAI.Chat.ChatCompletion>): OpenAI {
  return { chat: { completions: { create } } } as unknown as OpenAI;
}

function badRequest(): Error {
  return Object.assign(new Error("response_format is not supported"), { status: 400 });
}

beforeEach(() => resetEmitMode());

describe("llm_output", () => {
  it("returns a schema-valid emission from response_format", async () => {
    const bodies: Body[] = [];
    const client = clientOf(async (body) => {
      bodies.push(body);
      return completion({ role: "assistant", content: '{"action":"CONCLUDE"}' });
    });

    const result = await llm_output<{ action: string }>({
      client,
      model: "openai/gpt-4o",
      messages: [{ role: "user", content: "go" }],
      schema: SCHEMA,
      limiter: limiter(),
      rates: { input: 1, output: 1 },
    });

    expect(result.value.action).toBe("CONCLUDE");
    expect(result.rejected).toEqual([]);
    expect(bodies[0]!.response_format).toBeDefined();
    expect(result.cost_usd).toBeGreaterThan(0);

    // Unset, the gateway's own default cuts a long worker emission off mid-JSON,
    // which reaches the controller as an unparseable decision rather than as a
    // limit that was hit.
    expect(bodies[0]!.max_tokens).toBeGreaterThan(4096);
  });

  // Bifrost fronts providers whose native reply is a content-block list, and that
  // shape reaches us intact. Handed to JSON.parse it stringifies to [object
  // Object], so a correct decision would be discarded as invalid JSON.
  it("reads an emission returned as content blocks rather than a string", async () => {
    const client = clientOf(async () =>
      completion({
        role: "assistant",
        content: [{ type: "text", text: '{"action":' }, { type: "text", text: '"CONCLUDE"}' }],
      }),
    );

    const result = await llm_output<{ action: string }>({
      client,
      model: "anthropic/claude-opus-5",
      messages: [{ role: "user", content: "go" }],
      schema: SCHEMA,
      limiter: limiter(),
      rates: { input: 1, output: 1 },
    });

    expect(result.value.action).toBe("CONCLUDE");
    expect(result.rejected).toEqual([]);
  });

  // A model asked to fix an emission it cannot see has nothing to correct, and the
  // re-ask would otherwise land as a second consecutive user turn.
  it("feeds the rejected emission back as the assistant turn it was", async () => {
    const bodies: Body[] = [];
    let call = 0;
    const client = clientOf(async (body) => {
      bodies.push(body);
      call += 1;
      return completion({ role: "assistant", content: call === 1 ? "not json at all" : '{"action":"CONCLUDE"}' });
    });

    const result = await llm_output<{ action: string }>({
      client,
      model: "anthropic/claude-opus-5",
      messages: [{ role: "user", content: "go" }],
      schema: SCHEMA,
      limiter: limiter(),
      rates: { input: 1, output: 1 },
    });

    expect(result.value.action).toBe("CONCLUDE");
    const roles = bodies.at(-1)!.messages.map((message) => message.role);
    expect(roles).toEqual(["user", "assistant", "user", "user"]);
    expect(bodies.at(-1)!.messages[1]).toEqual({ role: "assistant", content: "not json at all" });
  });

  it("downgrades to a tool-shaped emit when the gateway rejects response_format", async () => {
    const bodies: Body[] = [];
    const client = clientOf(async (body) => {
      bodies.push(body);
      if (body.response_format !== undefined) throw badRequest();
      return completion({
        role: "assistant",
        tool_calls: [{ id: "1", type: "function", function: { name: EMIT_TOOL, arguments: '{"action":"CONCLUDE"}' } }],
      });
    });

    const options = {
      client,
      model: "openai/gpt-4o",
      messages: [{ role: "user" as const, content: "go" }],
      schema: SCHEMA,
      limiter: limiter(),
      rates: { input: 1, output: 1 },
    };
    expect((await llm_output<{ action: string }>(options)).value.action).toBe("CONCLUDE");

    // The downgrade is remembered, so the second call never probes again.
    await llm_output<{ action: string }>(options);
    expect(bodies.filter((body) => body.response_format !== undefined)).toHaveLength(1);
    expect(bodies.at(-1)!.tool_choice).toEqual({ type: "function", function: { name: EMIT_TOOL } });
  });

  it("re-prompts once on a schema violation and records the rejection", async () => {
    let call = 0;
    const client = clientOf(async () => {
      call += 1;
      const content = call === 1 ? '{"action":"NOPE"}' : '{"action":"CONCLUDE"}';
      return completion({ role: "assistant", content });
    });

    const result = await llm_output<{ action: string }>({
      client,
      model: "openai/gpt-4o",
      messages: [{ role: "user", content: "go" }],
      schema: SCHEMA,
      limiter: limiter(),
      rates: { input: 1, output: 1 },
    });

    expect(result.value.action).toBe("CONCLUDE");
    expect(result.rejected).toHaveLength(1);
    expect(result.rejected[0]).toContain("NOPE");
  });

  // Both gateway surfaces report the cached share under their own key, and a
  // re-prompt is a second billed call: totalling only the accepted one would
  // under-report exactly the turns the caching work is measured against.
  it("totals tokens across every billed turn, from either surface", async () => {
    let call = 0;
    const client = clientOf(async () => {
      call += 1;
      const usage =
        call === 1
          ? { prompt_tokens: 100, completion_tokens: 20, prompt_tokens_details: { cached_tokens: 60 } }
          : { prompt_tokens: 40, completion_tokens: 8, cache_read_input_tokens: 10, cache_creation_input_tokens: 30 };
      return {
        ...completion({ role: "assistant", content: call === 1 ? '{"action":"NOPE"}' : '{"action":"CONCLUDE"}' }),
        usage,
      } as unknown as OpenAI.Chat.ChatCompletion;
    });

    const result = await llm_output<{ action: string }>({
      client,
      model: "openai/gpt-4o",
      messages: [{ role: "user", content: "go" }],
      schema: SCHEMA,
      limiter: limiter(),
      rates: { input: 1, output: 1 },
    });

    expect(result.tokens).toEqual({ input: 140, output: 28, cache_read: 70, cache_write: 30 });
    // cache_read is a share of input, not an addition, so cost still prices input.
    expect(result.cost_usd).toBeCloseTo(168 / 1_000_000);
  });

  it("gives up rather than returning something off-schema", async () => {
    const client = clientOf(async () => completion({ role: "assistant", content: "not json at all" }));
    await expect(
      llm_output({
        client,
        model: "openai/gpt-4o",
        messages: [{ role: "user", content: "go" }],
        schema: SCHEMA,
        limiter: limiter(),
        rates: { input: 1, output: 1 },
      }),
    ).rejects.toThrow(LlmError);
  });

  it("does not swallow a non-400 failure as a schema downgrade", async () => {
    const client = clientOf(async () => {
      throw Object.assign(new Error("nope"), { status: 401 });
    });
    await expect(
      llm_output({
        client,
        model: "openai/gpt-4o",
        messages: [{ role: "user", content: "go" }],
        schema: SCHEMA,
        limiter: limiter(),
        rates: { input: 1, output: 1 },
      }),
    ).rejects.toThrow(/nope/);
  });
});

describe("renderDispatch", () => {
  it("gives a fanned-out worker its own focus", () => {
    const request: DispatchRequest = {
      dispatch_id: "dsp-1",
      hunt_id: "hunt-1",
      agent_id: "threat_hunter",
      query_intent: "characterise outbound traffic",
      focus: "check 10.0.0.1",
      target_hypothesis_id: "h-1",
      scope: { tenant: "acme" },
    };
    const rendered = renderDispatch(request, "scenario text");
    expect(rendered).toContain("characterise outbound traffic");
    expect(rendered).toContain("check 10.0.0.1");
    expect(rendered).toContain("h-1");
    expect(rendered).toContain("scenario text");
  });
});
