import { UnrecoverableError, type Job } from "bullmq";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RunJob } from "../../contracts/job.js";
import { httpAnnounce } from "../../core/checkpoints.js";
import { Limiter } from "../../core/limiter.js";
import { errorFields, logger } from "../../core/log.js";
import { httpPrices } from "../../core/prices.js";
import { SpecError } from "../../core/spec.js";
import { logFailure } from "../../worker.js";

let lines: Array<{ stream: "out" | "err"; entry: Record<string, unknown> }>;

beforeEach(() => {
  lines = [];
  const capture = (stream: "out" | "err") => (chunk: string | Uint8Array) => {
    lines.push({ stream, entry: JSON.parse(String(chunk)) as Record<string, unknown> });
    return true;
  };
  vi.spyOn(process.stdout, "write").mockImplementation(capture("out") as never);
  vi.spyOn(process.stderr, "write").mockImplementation(capture("err") as never);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

const RUN = "7f1c2d3e-0000-4000-8000-000000001585";

function job(attemptsMade: number, attempts = 3): Job<RunJob> {
  return { attemptsMade, opts: { attempts }, data: { run_id: RUN, run_kind: "hunt" } } as unknown as Job<RunJob>;
}

describe("the logger", () => {
  it("writes one JSON object per line with a constant msg and flat fields", () => {
    logger("agent.test").warn("something broke", { run_id: RUN, status: 502 });
    expect(lines).toHaveLength(1);
    expect(lines[0]?.stream).toBe("err");
    expect(Object.keys(lines[0]?.entry ?? {}).slice(0, 4)).toEqual(["ts", "level", "logger", "msg"]);
    expect(lines[0]?.entry).toMatchObject({ level: "warn", logger: "agent.test", msg: "something broke", run_id: RUN, status: 502 });
  });

  it("keeps a field from overwriting the line's own keys", () => {
    logger("agent.test").error("real", { msg: "forged", level: "debug" });
    expect(lines[0]?.entry).toMatchObject({ level: "error", msg: "real" });
  });

  it("drops what is below VIGIL_LOG_LEVEL and defaults to info", () => {
    const log = logger("agent.test");
    log.debug("quiet");
    log.info("loud", {});
    expect(lines.map((line) => line.entry["msg"])).toEqual(["loud"]);

    vi.stubEnv("VIGIL_LOG_LEVEL", "error");
    log.warn("hidden");
    log.error("shown");
    expect(lines.map((line) => line.entry["msg"])).toEqual(["loud", "shown"]);
  });

  it("names the error class and message apart", () => {
    expect(errorFields(new SpecError("bad spec"))).toEqual({ error_type: "SpecError", error: "bad spec" });
  });
});

describe("a failed run attempt", () => {
  it("warns while BullMQ will retry it", () => {
    logFailure(job(1), new Error("boom"));
    expect(lines[0]?.entry).toMatchObject({
      level: "warn",
      run_id: RUN,
      run_kind: "hunt",
      attempt: 1,
      attempts: 3,
      error_type: "Error",
      error: "boom",
    });
  });

  it("errors once retries are exhausted", () => {
    logFailure(job(3), new Error("boom"));
    expect(lines[0]?.entry).toMatchObject({ level: "error", msg: "run failed", attempt: 3, attempts: 3 });
  });

  it("errors at once on an UnrecoverableError, naming the class it wraps", () => {
    const wrapped = Object.assign(new UnrecoverableError("bad spec"), { cause: new SpecError("bad spec") });
    logFailure(job(1), wrapped);
    expect(lines[0]?.entry).toMatchObject({ level: "error", attempt: 1, error_type: "SpecError" });
  });

  it("copes with a job BullMQ no longer has", () => {
    logFailure(undefined, new Error("stalled"));
    expect(lines[0]?.entry).toMatchObject({ level: "error", error: "stalled" });
  });
});

describe("what the model limiter says", () => {
  const rate = { rpm: 1000, tpm: 1_000_000 };
  const rateLimited = () => Object.assign(new Error("slow down"), { status: 429, headers: { "retry-after": "0" } });

  it("warns on each retry and errors once on exhaustion", async () => {
    await expect(
      new Limiter(rate, 1, 2).run(1, async () => {
        throw rateLimited();
      }),
    ).rejects.toThrow("slow down");
    expect(lines.map((line) => [line.entry["level"], line.entry["status"]])).toEqual([
      ["warn", 429],
      ["error", 429],
    ]);
    expect(lines[0]?.entry).toMatchObject({ attempt: 1, attempts: 2, backoff_ms: 0 });
  });

  it("leaves a 402 to the worker's failed listener", async () => {
    await expect(
      new Limiter(rate, 1).run(1, async () => {
        throw Object.assign(new Error("out of budget"), { status: 402 });
      }),
    ).rejects.toThrow("out of budget");
    expect(lines).toEqual([]);
  });
});

describe("the ports that fail open", () => {
  it("warns once per failed price lookup and still answers null", async () => {
    const prices = httpPrices({
      url: "http://backend/internal/pricing",
      token: "secret",
      ttlMs: 1000,
      fetch: (async () => new Response(null, { status: 503 })) as typeof globalThis.fetch,
    });
    expect(await prices("gemini/flash", "gemini")).toBeNull();
    expect(lines).toHaveLength(1);
    expect(lines[0]?.entry).toMatchObject({ level: "warn", model_id: "gemini/flash", status: 503 });
    expect(JSON.stringify(lines[0]?.entry)).not.toContain("secret");
  });

  it("puts the checkpoint id in a field and not in the message", async () => {
    const announce = httpAnnounce({
      url: "http://backend/internal/runs",
      token: "t",
      fetch: (async () => {
        throw new Error("connection refused");
      }) as typeof globalThis.fetch,
    });
    await announce(RUN, "hunt", { checkpoint_id: "apr-1", checkpoint_class: "tool_approval", question: "?", raised_at: "" });
    expect(lines[0]?.entry).toMatchObject({
      level: "warn",
      msg: "announcing a checkpoint failed",
      run_id: RUN,
      checkpoint_id: "apr-1",
      error: "connection refused",
    });
  });
});
