import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { GatewayExhausted, Limiter } from "../../../core/limiter.js";
import { bifrostMessages, GatewayError } from "../../../plugins/rootcause/v3/model/bifrost.js";
import type { Message, MessageRequest } from "../../../plugins/rootcause/v3/model/messages.js";

// Responses recorded from Bifrost's /anthropic/v1/messages in front of Claude on Vertex.
const recorded = (name: string): Message => JSON.parse(readFileSync(new URL(`./fixtures/bifrost/${name}.json`, import.meta.url), "utf8"));

const request: MessageRequest = {
  model: "claude-opus-4-8",
  max_tokens: 16000,
  system: [{ type: "text", text: "rules" }, { type: "text", text: "survey", cache_control: { type: "ephemeral" } }],
  tools: [{ name: "submit_test", description: "d", input_schema: { type: "object" } }],
  messages: [{ role: "user", content: [{ type: "text", text: "go", cache_control: { type: "ephemeral" } }] }],
  thinking: { type: "adaptive", display: "summarized" },
};

function gateway(answers: (Response | Error)[]) {
  const sent: { url: string; init: RequestInit }[] = [];
  const fetch = async (url: string | URL | Request, init?: RequestInit) => {
    sent.push({ url: String(url), init: init ?? {} });
    const next = answers.shift();
    if (next === undefined) throw new Error("no more answers");
    if (next instanceof Error) throw next;
    return next;
  };
  return { sent, fetch: fetch as typeof globalThis.fetch };
}

const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
const limiter = () => new Limiter({ rpm: 1000, tpm: 1_000_000 }, 2);

describe("bifrostMessages", () => {
  it("posts the loop's request to the Anthropic surface unchanged, under the gateway's model name and the budget key", async () => {
    const { sent, fetch } = gateway([ok(recorded("forced-submit"))]);
    const messages = bifrostMessages({ baseUrl: "http://bifrost:8080/", apiKey: "k", provider: "vertex", vk: async () => "vk-1", limiter: limiter(), fetch });
    await messages.create(request);

    expect(sent).toHaveLength(1);
    expect(sent[0]!.url).toBe("http://bifrost:8080/anthropic/v1/messages");
    const headers = sent[0]!.init.headers as Record<string, string>;
    expect(headers).toMatchObject({ "anthropic-version": "2023-06-01", authorization: "Bearer k", "x-bf-vk": "vk-1" });
    const body = JSON.parse(String(sent[0]!.init.body));
    expect(body.model).toBe("vertex/claude-opus-4-8");
    // What the cost and the loop's behaviour rest on: the cache marks and adaptive thinking.
    expect(body.system[1].cache_control).toEqual({ type: "ephemeral" });
    expect(body.messages[0].content[0].cache_control).toEqual({ type: "ephemeral" });
    expect(body.thinking).toEqual({ type: "adaptive", display: "summarized" });
  });

  it("sends no budget key when there is none", async () => {
    const { sent, fetch } = gateway([ok(recorded("forced-submit"))]);
    await bifrostMessages({ baseUrl: "http://bifrost:8080", limiter: limiter(), fetch }).create(request);
    expect(sent[0]!.init.headers).not.toHaveProperty("x-bf-vk");
  });

  it("retries a gateway that stumbled, through the shared limiter", async () => {
    const { sent, fetch } = gateway([new Response("overloaded", { status: 503, headers: { "retry-after": "0" } }), ok(recorded("forced-submit"))]);
    const message = await bifrostMessages({ baseUrl: "http://bifrost:8080", limiter: limiter(), fetch }).create(request);
    expect(sent).toHaveLength(2);
    expect(message.stop_reason).toBe("tool_use");
  });

  it("retries an overloaded model (529) rather than ending the run", async () => {
    const overloaded = JSON.stringify({ type: "error", error: { type: "overloaded_error", message: "Overloaded" } });
    const { sent, fetch } = gateway([new Response(overloaded, { status: 529, headers: { "retry-after": "0" } }), ok(recorded("forced-submit"))]);
    const message = await bifrostMessages({ baseUrl: "http://bifrost:8080", limiter: limiter(), fetch }).create(request);
    expect(sent).toHaveLength(2);
    expect(message.stop_reason).toBe("tool_use");
  });

  it("stops at once when the gateway's budget is spent", async () => {
    const { fetch } = gateway([new Response("budget exceeded", { status: 402 })]);
    await expect(bifrostMessages({ baseUrl: "http://bifrost:8080", limiter: limiter(), fetch }).create(request)).rejects.toBeInstanceOf(GatewayExhausted);
  });

  it("does not retry a request the gateway refused", async () => {
    const { sent, fetch } = gateway([new Response("bad tool schema", { status: 400 })]);
    await expect(bifrostMessages({ baseUrl: "http://bifrost:8080", limiter: limiter(), fetch }).create(request)).rejects.toBeInstanceOf(GatewayError);
    expect(sent).toHaveLength(1);
  });
});

// The loop reads blocks by type, the stop reason and four usage counters. Held to what
// Bifrost actually returned, so a change in its surface shows here rather than mid-run.
describe("Bifrost's recorded answers fit the loop's Message", () => {
  const blocks = (m: Message) => m.content.map((b) => b.type);

  it.each(["thinking-tool-use", "forced-submit", "thinking-text"])("%s", (name) => {
    const m = recorded(name);
    for (const block of m.content) expect(["text", "thinking", "tool_use"]).toContain(block.type);
    expect(typeof m.stop_reason).toBe("string");
    expect(typeof m.usage.input_tokens).toBe("number");
    expect(typeof m.usage.output_tokens).toBe("number");
    expect(typeof m.usage.cache_read_input_tokens).toBe("number");
    expect(typeof m.usage.cache_creation_input_tokens).toBe("number");
  });

  it("a forced submit answers with only the tool call", () => {
    const m = recorded("forced-submit");
    expect(blocks(m)).toEqual(["tool_use"]);
    expect(m.content[0]).toMatchObject({ type: "tool_use", name: "submit_assessment" });
  });

  it("a cached prefix was written on the first call", () => {
    expect(recorded("thinking-tool-use").usage.cache_creation_input_tokens).toBeGreaterThan(1024);
  });

  it("adaptive thinking comes back as a signed thinking block", () => {
    const thinking = recorded("thinking-text").content.find((b) => b.type === "thinking");
    expect(thinking).toMatchObject({ type: "thinking" });
    expect(thinking && "signature" in thinking && typeof thinking.signature).toBe("string");
  });
});
