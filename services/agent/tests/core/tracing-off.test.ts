import { describe, expect, it } from "vitest";
import { defineTool, type ToolResult } from "../../contracts/tool.js";
import { remoteDispatch } from "../../core/remote.js";
import { initTracing, traced, traceHeaders } from "../../core/tracing.js";

// No provider registered, as in a deployment that never set VIGIL_OTEL_ENABLED.
describe("with tracing off", () => {
  it("does not start", () => {
    expect(initTracing("vigil-agent-test")).toBe(false);
  });

  it("runs the call and adds nothing to what crosses the boundary", async () => {
    const tool = defineTool(
      {
        id: "list_findings",
        description: "findings, most recent first",
        parameters: { type: "object", properties: {} },
        execute: async (): Promise<ToolResult> => ({ ok: false, failure: { kind: "unavailable", detail: "never local" } }),
      },
      { maxRows: 2, timeoutMs: 5_000 },
    );
    const sent: Request[] = [];
    const fetch = (async (url: string, init: RequestInit) => {
      sent.push(new Request(url, init));
      return new Response(JSON.stringify({ ok: true, rows: [], rowCount: 0, capped: false, sourceSystem: "vigil" }));
    }) as unknown as typeof globalThis.fetch;

    const result = await traced("run", {}, () =>
      remoteDispatch({ url: "http://127.0.0.1:6987/internal/tools/invoke", token: "shhh", fetch }).invoke(tool, {}),
    );

    expect(result.ok).toBe(true);
    expect(sent[0]!.headers.get("traceparent")).toBeNull();
    expect(traceHeaders()).toEqual({});
  });
});
