import { describe, expect, it } from "vitest";
import { defineTool } from "../../contracts/tool.js";
import type { ToolResult } from "../../contracts/tool.js";
import { getEventListeners } from "node:events";
import { remoteDispatch } from "../../core/remote.js";

const TOOL = defineTool(
  {
    id: "list_findings",
    description: "findings, most recent first",
    parameters: { type: "object", properties: {} },
    execute: async (): Promise<ToolResult> => ({ ok: false, failure: { kind: "unavailable", detail: "never local" } }),
  },
  { maxRows: 2, timeoutMs: 5_000 },
);

function answering(body: unknown, status = 200): { fetch: typeof globalThis.fetch; sent: Request[] } {
  const sent: Request[] = [];
  const fetch = (async (url: string, init: RequestInit) => {
    sent.push(new Request(url, init));
    return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  }) as unknown as typeof globalThis.fetch;
  return { fetch, sent };
}

function dispatchTo(fetch: typeof globalThis.fetch) {
  return remoteDispatch({ url: "http://127.0.0.1:6987/internal/tools/invoke", token: "shhh", fetch });
}

// Every tool call routes through this dispatch, so a local one posted remotely is
// refused by a backend that has never heard of the run's own ledger -- which is
// what happened to expand on every hunt.
describe("a tool this process answers itself", () => {
  const LOCAL = defineTool(
    {
      id: "expand",
      description: "the raw payloads behind evidence ids from this run's own record",
      parameters: { type: "object", properties: {} },
      execute: async (): Promise<ToolResult> => ({ ok: true, rows: [{ evidence_id: "ev-1" }], rowCount: 1, capped: false, sourceSystem: "ledger" }),
    },
    { maxRows: 2, timeoutMs: 5_000 },
    true,
  );

  it("runs in process and posts nothing", async () => {
    const { fetch, sent } = answering({ ok: false, failure: { kind: "refused", detail: "no such tool: expand" } });
    const result = await dispatchTo(fetch).invoke(LOCAL, { evidence_ids: ["ev-1"] });

    expect(sent).toHaveLength(0);
    expect(result).toEqual({ ok: true, rows: [{ evidence_id: "ev-1" }], rowCount: 1, capped: false, sourceSystem: "ledger" });
  });
});

describe("a tool that runs in the other process", () => {
  it("sends the tool, its arguments and its bounds", async () => {
    const { fetch, sent } = answering({ ok: true, rows: [], rowCount: 0, capped: false, sourceSystem: "vigil" });
    await dispatchTo(fetch).invoke(TOOL, { severity: "high" });

    expect(sent).toHaveLength(1);
    expect(await sent[0]!.json()).toEqual({
      tool: "list_findings",
      args: { severity: "high" },
      bounds: { max_rows: 2, timeout_ms: 5_000 },
    });
  });

  it("carries the shared secret and nothing else identifying", async () => {
    const { fetch, sent } = answering({ ok: true, rows: [], rowCount: 0, capped: false, sourceSystem: "vigil" });
    await dispatchTo(fetch).invoke(TOOL, {});
    expect(sent[0]!.headers.get("authorization")).toBe("Bearer shhh");
  });

  it("returns rows the far side capped, saying they were capped", async () => {
    const body = { ok: true, rows: [{ id: 1 }, { id: 2 }], rowCount: 2, capped: true, sourceSystem: "vigil" };
    const result = await dispatchTo(answering(body).fetch).invoke(TOOL, {});
    expect(result).toEqual(body);
  });
});

describe("the failure kind survives the hop", () => {
  it.each(["invalid_args", "refused", "unavailable", "backend_error"] as const)("relays %s as itself", async (kind) => {
    const { fetch } = answering({ ok: false, failure: { kind, detail: "as reported" } });
    const result = await dispatchTo(fetch).invoke(TOOL, {});
    expect(result).toEqual({ ok: false, failure: { kind, detail: "as reported" } });
  });

  it("relays a timeout as a timeout rather than as a backend error", async () => {
    const { fetch } = answering({ ok: false, failure: { kind: "timeout", timeoutMs: 5_000 } });
    const result = await dispatchTo(fetch).invoke(TOOL, {});
    expect(result).toEqual({ ok: false, failure: { kind: "timeout", timeoutMs: 5_000 } });
  });

  it("does not invent a kind the far side did not send", async () => {
    const { fetch } = answering({ ok: false, failure: { kind: "made_up", detail: "no" } });
    const result = await dispatchTo(fetch).invoke(TOOL, {});
    expect(result).toEqual({ ok: false, failure: { kind: "unavailable", detail: expect.stringContaining("known failure") } });
  });
});

describe("the hop failing is not the tool failing", () => {
  it("reads a transport error as unavailable, which is a visibility gap", async () => {
    const fetch = (async () => {
      throw new Error("ECONNREFUSED");
    }) as unknown as typeof globalThis.fetch;
    const result = await dispatchTo(fetch).invoke(TOOL, {});
    expect(result).toEqual({ ok: false, failure: { kind: "unavailable", detail: "ECONNREFUSED" } });
  });

  it("reads a non-200 as unavailable and names the status", async () => {
    const { fetch } = answering({ nope: true }, 500);
    const result = await dispatchTo(fetch).invoke(TOOL, {});
    expect(result).toEqual({ ok: false, failure: { kind: "unavailable", detail: "the endpoint answered 500" } });
  });

  it("reads a 401 as unavailable rather than as a refusal by the tool", async () => {
    const { fetch } = answering({ detail: "bad token" }, 401);
    const result = await dispatchTo(fetch).invoke(TOOL, {});
    expect(result).toEqual({ ok: false, failure: { kind: "unavailable", detail: "the endpoint answered 401" } });
  });
});

// A run's signal outlives every tool call made under it, so anything attached
// per call and left there accumulates for the life of the run.
describe("the run's signal is not a place listeners accumulate", () => {
  it("attaches for the call and lets go when it ends", async () => {
    let during = -1;
    const run = new AbortController();
    const attached = () => getEventListeners(run.signal, "abort").length;
    const fetch = (async () => {
      during = attached();
      return new Response(JSON.stringify({ rows: [], row_count: 0, source_system: "vigil" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }) as unknown as typeof globalThis.fetch;

    for (let call = 0; call < 20; call += 1) await dispatchTo(fetch).invoke(TOOL, {}, run.signal);

    expect(during).toBe(1);
    expect(attached()).toBe(0);
  });

  it("still stops a call the run aborted", async () => {
    const fetch = (async (_url: string, init: RequestInit) =>
      await new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
      })) as unknown as typeof globalThis.fetch;

    const run = new AbortController();
    const inflight = dispatchTo(fetch).invoke(TOOL, {}, run.signal);
    run.abort();

    expect((await inflight).ok).toBe(false);
  });
});
