import { SpanKind, SpanStatusCode } from "@opentelemetry/api";
import { InMemorySpanExporter, NodeTracerProvider, SimpleSpanProcessor } from "@opentelemetry/sdk-trace-node";
import type { ServerResponse } from "node:http";
import type OpenAI from "openai";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { RunJob } from "../../contracts/job.js";
import { defineTool, type ToolResult } from "../../contracts/tool.js";
import { Limiter } from "../../core/limiter.js";
import { remoteDispatch } from "../../core/remote.js";
import { InProcessState } from "../../core/state.js";
import { traced } from "../../core/tracing.js";
import { openAiSurface } from "../../core/wire.js";
import type { Message } from "../../core/provider.js";
import { streamChat } from "../../serve.js";
import { handle } from "../../worker.js";
import { InProcessDirectiveQueue } from "../../plugins/hunt/directives.js";
import { InProcessLeases } from "../support/in-process-leases.js";

const exporter = new InMemorySpanExporter();
const provider = new NodeTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] });

beforeAll(() => provider.register());
afterEach(() => {
  exporter.reset();
  vi.unstubAllEnvs();
});
afterAll(() => provider.shutdown());

function only() {
  const spans = exporter.getFinishedSpans();
  expect(spans).toHaveLength(1);
  return spans[0]!;
}

const TOOL = defineTool(
  {
    id: "list_findings",
    description: "findings, most recent first",
    parameters: { type: "object", properties: {} },
    execute: async (): Promise<ToolResult> => ({ ok: false, failure: { kind: "unavailable", detail: "never local" } }),
  },
  { maxRows: 2, timeoutMs: 5_000 },
);

function answering(body: unknown): { fetch: typeof globalThis.fetch; sent: Request[] } {
  const sent: Request[] = [];
  const fetch = (async (url: string, init: RequestInit) => {
    sent.push(new Request(url, init));
    return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
  }) as unknown as typeof globalThis.fetch;
  return { fetch, sent };
}

function dispatchTo(fetch: typeof globalThis.fetch) {
  return remoteDispatch({ url: "http://127.0.0.1:6987/internal/tools/invoke", token: "shhh", fetch });
}

describe("a tool call", () => {
  const ROWS: ToolResult = { ok: true, rows: [{ id: "f-1" }], rowCount: 1, capped: false, sourceSystem: "vigil" };

  it("is a span named for the tool, carried to the backend in the request", async () => {
    const { fetch, sent } = answering(ROWS);
    await dispatchTo(fetch).invoke(TOOL, { severity: "high" }, undefined, "agent-7");

    const span = only();
    expect(span.name).toBe("execute_tool list_findings");
    expect(span.attributes).toMatchObject({
      "gen_ai.operation.name": "execute_tool",
      "gen_ai.tool.name": "list_findings",
      "gen_ai.agent.id": "agent-7",
    });
    const { traceId, spanId } = span.spanContext();
    expect(sent[0]!.headers.get("traceparent")).toBe(`00-${traceId}-${spanId}-01`);
  });

  it("carries no arguments or result unless content capture is on", async () => {
    await dispatchTo(answering(ROWS).fetch).invoke(TOOL, { severity: "high" });
    expect(only().attributes["gen_ai.tool.call.arguments"]).toBeUndefined();
    exporter.reset();

    vi.stubEnv("VIGIL_OTEL_CAPTURE_CONTENT", "true");
    await dispatchTo(answering(ROWS).fetch).invoke(TOOL, { severity: "high" });
    expect(only().attributes).toMatchObject({
      "gen_ai.tool.call.arguments": JSON.stringify({ severity: "high" }),
      "gen_ai.tool.call.result": JSON.stringify(ROWS),
    });
  });

  // The model reads a failed tool as a result, so nothing throws; the span still says so.
  it("is marked failed when the tool answers with a failure", async () => {
    await dispatchTo(answering({ ok: false, failure: { kind: "refused", detail: "no" } }).fetch).invoke(TOOL, {});
    expect(only().status).toEqual({ code: SpanStatusCode.ERROR, message: "refused" });
  });
});

describe("a model call", () => {
  function surface() {
    const create = async () => ({
      async *[Symbol.asyncIterator]() {
        yield { id: "c", created: 1, model: "gpt-4o-2024", object: "chat.completion.chunk", choices: [{ index: 0, delta: { content: "hello" }, finish_reason: "stop" }] };
        yield { id: "c", created: 1, model: "gpt-4o-2024", object: "chat.completion.chunk", choices: [], usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } };
      },
    });
    const client = { chat: { completions: { create } } } as unknown as OpenAI;
    return openAiSurface(client, "gpt-4o", new Limiter({ rpm: 10_000, tpm: 10_000_000 }, 4, 1), "openai", "openai/gpt-4o");
  }

  async function ask(messages: Message[] = [{ role: "user", content: "hi" }]): Promise<void> {
    for await (const _ of surface().stream({ messages, tools: [] })) void _;
  }

  it("is a client span with the model, its usage and when the limiter let it through", async () => {
    await ask();

    const span = only();
    expect(span.name).toBe("chat openai/gpt-4o");
    expect(span.kind).toBe(SpanKind.CLIENT);
    expect(span.attributes).toMatchObject({
      "gen_ai.operation.name": "chat",
      "gen_ai.provider.name": "openai",
      "gen_ai.request.model": "openai/gpt-4o",
      "gen_ai.response.model": "gpt-4o-2024",
      "gen_ai.response.finish_reasons": ["stop"],
      "gen_ai.usage.input_tokens": 10,
      "gen_ai.usage.output_tokens": 5,
    });
    expect(span.attributes["gen_ai.input.messages"]).toBeUndefined();
    expect(span.events.map((event) => event.name)).toEqual(["admitted"]);
  });

  it("carries the messages both ways when content capture is on", async () => {
    vi.stubEnv("VIGIL_OTEL_CAPTURE_CONTENT", "true");
    await ask();

    const { attributes } = only();
    expect(JSON.parse(attributes["gen_ai.input.messages"] as string)).toEqual([{ role: "user", content: "hi" }]);
    expect(JSON.parse(attributes["gen_ai.output.messages"] as string)).toMatchObject([{ role: "assistant", content: "hello" }]);
  });

  // The cap cuts from the end, so a whole history would keep its oldest turns.
  it("carries only what was added since the model last spoke", async () => {
    vi.stubEnv("VIGIL_OTEL_CAPTURE_CONTENT", "true");
    await ask([
      { role: "user", content: "first" },
      { role: "assistant", content: "earlier answer", tool_calls: [] },
      { role: "user", content: "second" },
    ]);

    expect(JSON.parse(only().attributes["gen_ai.input.messages"] as string)).toEqual([{ role: "user", content: "second" }]);
  });
});

describe("a run's root span", () => {
  const TRACE = "0af7651916cd43dd8448eb211c80319c";
  const TRACEPARENT = `00-${TRACE}-b7ad6b7169203331-01`;

  it("continues the trace the job was enqueued under, and fails with the job", async () => {
    const job: RunJob = {
      schema_version: 1,
      run_id: "5a2c2d3e-0000-4000-8000-000000000777",
      run_kind: "hunt",
      tenant_id: null,
      enqueued_at: new Date().toISOString(),
      enqueued_by: "test",
      traceparent: TRACEPARENT,
      reason: "start",
      request: { arch: "", playbook: "/nonexistent/hunt.playbook.yaml", config: "", prompt: "go" },
    };
    await expect(handle(new InProcessState(), new InProcessLeases(), job, new InProcessDirectiveQueue())).rejects.toThrow();

    const span = only();
    expect(span.name).toBe("invoke_agent hunt");
    expect(span.spanContext().traceId).toBe(TRACE);
    expect(span.attributes["vigil.run_id"]).toBe(job.run_id);
    expect(span.status.code).toBe(SpanStatusCode.ERROR);
  });

  // streamChat answers a failure with an error frame rather than a throw.
  it("is marked failed by a chat turn that failed without throwing", async () => {
    const res = { write: () => true, end: () => undefined, writableEnded: false, destroyed: false } as unknown as ServerResponse;
    const request = { run_id: "5a2c2d3e-0000-4000-8000-000000000778", turns: [], system_prompt: "", config: "model: x\ntools: [oops]" };
    await traced("invoke_agent chat", {}, () => streamChat(new InProcessState(), request, res));

    expect(only().status.code).toBe(SpanStatusCode.ERROR);
  });
});

describe("traced", () => {
  // What a job enqueued by the API carries, so a run continues the request's trace.
  it("continues the trace a carrier names", async () => {
    const traceId = "0af7651916cd43dd8448eb211c80319c";
    await traced("run", {}, async () => undefined, { parent: { traceparent: `00-${traceId}-b7ad6b7169203331-01` } });

    const span = only();
    expect(span.spanContext().traceId).toBe(traceId);
    expect(span.parentSpanContext?.spanId).toBe("b7ad6b7169203331");
  });

  it("starts a trace of its own when the carrier names none", async () => {
    await traced("run", {}, async () => undefined, { parent: { traceparent: undefined } });
    expect(only().parentSpanContext).toBeUndefined();
  });

  it("ends the span failed and rethrows when fn throws", async () => {
    await expect(traced("run", {}, async () => Promise.reject(new Error("boom")))).rejects.toThrow("boom");

    const span = only();
    expect(span.status).toEqual({ code: SpanStatusCode.ERROR, message: "boom" });
    expect(span.events.map((event) => event.name)).toEqual(["exception"]);
  });
});
