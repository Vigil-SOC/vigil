import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { announceStart } from "../../worker.js";

const URL = "http://backend.internal:6987/internal/runs";
let lines: Array<{ stream: "out" | "err"; raw: string; entry: Record<string, unknown> }>;

beforeEach(() => {
  lines = [];
  const capture = (stream: "out" | "err") => (chunk: string | Uint8Array) => {
    const raw = String(chunk);
    lines.push({ stream, raw, entry: JSON.parse(raw) as Record<string, unknown> });
    return true;
  };
  vi.spyOn(process.stdout, "write").mockImplementation(capture("out") as never);
  vi.spyOn(process.stderr, "write").mockImplementation(capture("err") as never);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

const started = () => lines.filter((line) => line.entry["msg"] === "agent worker started");

describe("the worker's start line", () => {
  it("says mirror on, logs no ERROR, and leaks no URL when VIGIL_RUNS_URL is set", () => {
    vi.stubEnv("VIGIL_RUNS_URL", URL);
    announceStart();
    expect(started()).toHaveLength(1);
    expect(started()[0]?.entry).toMatchObject({ level: "info", mirror: "on", queue: "agent-runs", concurrency: expect.any(Number), health_port: expect.any(Number) });
    expect(lines.filter((line) => line.entry["level"] === "error")).toEqual([]);
    expect(lines.some((line) => line.raw.includes(URL) || line.raw.includes("backend.internal"))).toBe(false);
  });

  it.each([["unset", undefined], ["empty", ""]])("keeps the ERROR and says mirror off when VIGIL_RUNS_URL is %s", (_, value) => {
    vi.stubEnv("VIGIL_RUNS_URL", value);
    announceStart();
    const errors = lines.filter((line) => line.entry["level"] === "error");
    expect(errors.map((line) => line.entry["msg"])).toEqual([
      "VIGIL_RUNS_URL is unset: run progress will not be mirrored to the backend and checkpoints cannot be answered",
    ]);
    expect(started()).toHaveLength(1);
    expect(started()[0]?.entry).toMatchObject({ level: "info", mirror: "off" });
  });
});
