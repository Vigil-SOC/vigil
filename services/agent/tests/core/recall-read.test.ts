import { describe, expect, it } from "vitest";
import { httpRecall } from "../../core/recall.js";
import { nullMemory, recalling } from "../../core/memory.js";
import { RECALL_TOOL } from "../../contracts/memory.js";
import { recalledFixture, RECALL_KEYS } from "../support/recalled.js";

const URL = "http://localhost:6987/internal/tools/invoke";
const AS_OF = "2026-03-07T00:00:00.000Z";
const RECALLED = recalledFixture();

interface Sent {
  url: string;
  headers: Record<string, string>;
  body: Record<string, unknown>;
}

// Answers what it is given, and records what it was asked, so a test asserts on
// the request as well as on what came back.
function answering(reply: () => Response): { fetch: typeof globalThis.fetch; sent: () => Sent[] } {
  const sent: Sent[] = [];
  return {
    sent: () => sent,
    fetch: (async (url: string, init: RequestInit) => {
      sent.push({
        url: String(url),
        headers: (init.headers ?? {}) as Record<string, string>,
        body: JSON.parse(String(init.body)) as Record<string, unknown>,
      });
      return reply();
    }) as unknown as typeof globalThis.fetch,
  };
}

const ok = (body: unknown): Response => new Response(JSON.stringify(body), { status: 200 });

// Answers only when the request is aborted, which is what a far side that has
// stopped answering looks like from here.
const hanging = ((_url: string, init: RequestInit) =>
  new Promise<Response>((_keep, fail) => {
    init.signal?.addEventListener("abort", () => fail(new Error("aborted")), { once: true });
  })) as unknown as typeof globalThis.fetch;

const carried = (result = RECALLED): Response => ok({ ok: true, rows: [result], rowCount: 1, capped: false, sourceSystem: "memory" });

describe("the keyed read over the tool bridge", () => {
  it("asks the one endpoint for the one tool", async () => {
    const answers = answering(() => carried());
    const memory = httpRecall(nullMemory, { url: URL, token: "t", fetch: answers.fetch });
    await memory.entities({ keys: RECALL_KEYS, asOf: AS_OF, runId: "run-1" });

    const [call] = answers.sent();
    expect(call?.url).toBe(URL);
    expect(call?.body["tool"]).toBe(RECALL_TOOL);
  });

  it("names the run as the caller, so the read log attributes it", async () => {
    const answers = answering(() => carried());
    const memory = httpRecall(nullMemory, { url: URL, token: "t", fetch: answers.fetch });
    await memory.entities({ keys: RECALL_KEYS, asOf: AS_OF, runId: "run-7" });

    expect(answers.sent()[0]?.body["args"]).toEqual({
      entity_keys: [...RECALL_KEYS],
      as_of: AS_OF,
      caller_kind: "run",
      caller_id: "run-7",
    });
  });

  // The endpoint declares bounds required and both fields positive
  // (core/agents/tools_router.py Bounds), so a body without them is refused before
  // the tool runs -- and every read would be journaled unavailable.
  it("carries the bounds the endpoint requires", async () => {
    const answers = answering(() => carried());
    const memory = httpRecall(nullMemory, { url: URL, token: "t", fetch: answers.fetch });
    await memory.entities({ keys: RECALL_KEYS, asOf: AS_OF, runId: "run-1" });

    expect(answers.sent()[0]?.body["bounds"]).toEqual({ max_rows: 1, timeout_ms: 10_000 });
  });

  it("carries the internal token", async () => {
    const answers = answering(() => carried());
    const memory = httpRecall(nullMemory, { url: URL, token: "secret", fetch: answers.fetch });
    await memory.entities({ keys: RECALL_KEYS, asOf: AS_OF, runId: "run-1" });
    expect(answers.sent()[0]?.headers["authorization"]).toBe("Bearer secret");
  });

  it("returns the rows the endpoint carried", async () => {
    const memory = httpRecall(nullMemory, { url: URL, token: "t", fetch: answering(() => carried()).fetch });
    expect(await memory.entities({ keys: RECALL_KEYS, asOf: AS_OF, runId: "run-1" })).toEqual(RECALLED);
  });

  // Every one of these reaches the harness as a throw, which journals the read as
  // unavailable. Answering an empty result instead would say these keys have no
  // history, which is a different fact.
  it("refuses an answer that is not a result", async () => {
    const memory = httpRecall(nullMemory, { url: URL, token: "t", fetch: answering(() => ok({ ok: true, rows: [] })).fetch });
    await expect(memory.entities({ keys: RECALL_KEYS, asOf: AS_OF, runId: "run-1" })).rejects.toThrow();
  });

  it("refuses a row that is not a recall", async () => {
    const answers = answering(() => ok({ ok: true, rows: [{ nothing: "useful" }], rowCount: 1, capped: false, sourceSystem: "memory" }));
    const memory = httpRecall(nullMemory, { url: URL, token: "t", fetch: answers.fetch });
    await expect(memory.entities({ keys: RECALL_KEYS, asOf: AS_OF, runId: "run-1" })).rejects.toThrow();
  });

  it("says what the endpoint answered when it refuses", async () => {
    const memory = httpRecall(nullMemory, { url: URL, token: "t", fetch: answering(() => new Response("", { status: 503 })).fetch });
    await expect(memory.entities({ keys: RECALL_KEYS, asOf: AS_OF, runId: "run-1" })).rejects.toThrow(/503/);
  });

  it("refuses a tool failure rather than reading it as no history", async () => {
    const answers = answering(() => ok({ ok: false, failure: { kind: "backend_error", detail: "the table is gone" } }));
    const memory = httpRecall(nullMemory, { url: URL, token: "t", fetch: answers.fetch });
    await expect(memory.entities({ keys: RECALL_KEYS, asOf: AS_OF, runId: "run-1" })).rejects.toThrow(/the table is gone/);
  });

  it("does not ask at all when the run named no keys", async () => {
    const answers = answering(() => carried());
    const memory = httpRecall(nullMemory, { url: URL, token: "t", fetch: answers.fetch });
    const result = await memory.entities({ keys: [], asOf: AS_OF, runId: "run-1" });

    expect(answers.sent()).toEqual([]);
    expect(result.sightings).toEqual([]);
    expect(result.keys).toEqual([]);
  });

  // The two tiers are different reads. Wrapping must not cost a run the notes its
  // parent carried forward.
  it("leaves the tier underneath it alone", async () => {
    const base = recalling(async (limit) => ["what the parent concluded"].slice(0, limit));
    const memory = httpRecall(base, { url: URL, token: "t", fetch: answering(() => carried()).fetch });
    expect(await memory.recall("cue", 3)).toEqual(["what the parent concluded"]);
  });

  // A run that lost its lease must not wait out a memory read, which is the reason
  // remote.ts composes the two signals rather than trusting the timeout alone.
  it("lets go when the run does, without waiting for its own deadline", async () => {
    const lease = new AbortController();
    const memory = httpRecall(nullMemory, { url: URL, token: "t", fetch: hanging, timeoutMs: 600_000 });
    const read = memory.entities({ keys: RECALL_KEYS, asOf: AS_OF, runId: "run-1", signal: lease.signal });
    lease.abort(new Error("the worker lost its lease"));

    await expect(read).rejects.toThrow(/lease/);
  });

  it("gives up rather than holding a run's opening turn open", async () => {
    const memory = httpRecall(nullMemory, { url: URL, token: "t", fetch: hanging, timeoutMs: 5 });
    await expect(memory.entities({ keys: RECALL_KEYS, asOf: AS_OF, runId: "run-1" })).rejects.toThrow(/5ms/);
  });
});
