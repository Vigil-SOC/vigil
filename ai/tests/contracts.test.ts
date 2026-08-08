import { describe, expect, it } from "vitest";
import {
  defineTool,
  ToolBoundsViolation,
  type RegisteredTool,
  type ToolAdapter,
  type ToolResult,
} from "../contracts/tool.js";
import { RUN_EVENT_KINDS, isRunEventKind, type AgentEvent } from "../contracts/events.js";
import { jobIdFor, type RunJob } from "../contracts/job.js";
import { rateTableOf } from "../contracts/rates.js";

const rows = (n: number, sourceSystem = "duckdb"): ToolResult => ({
  ok: true,
  rows: Array.from({ length: n }, (_, i) => ({ i })),
  rowCount: n,
  capped: false,
  sourceSystem,
});

function adapter(execute: ToolAdapter["execute"]): ToolAdapter {
  return { id: "probe", description: "test adapter", parameters: {}, execute };
}

const START: RunJob = {
  schema_version: 1,
  run_id: "8f1c2d3e-0000-4000-8000-000000000001",
  run_kind: "hunt",
  tenant_id: null,
  enqueued_at: "2026-08-07T00:00:00.000Z",
  enqueued_by: "user-1",
  reason: "start",
  request: { arch: "arch/threathunt.yaml", playbook: "demo.yaml", config: "vigil.config.yaml", prompt: "go" },
};

const RESUME: RunJob = {
  schema_version: START.schema_version,
  run_id: START.run_id,
  run_kind: START.run_kind,
  tenant_id: START.tenant_id,
  enqueued_at: START.enqueued_at,
  enqueued_by: "watchdog",
  reason: "resume",
};

describe("the Tool port cannot be opted out of", () => {
  it("passes its bounds to the adapter", async () => {
    let seen: { maxRows: number; timeoutMs: number } | null = null;
    const tool = defineTool(
      adapter(async (_args, bounds) => {
        seen = bounds;
        return rows(1);
      }),
      { maxRows: 50, timeoutMs: 1_000 },
    );
    await tool.invoke({});
    expect(seen).toEqual({ maxRows: 50, timeoutMs: 1_000 });
  });

  it("throws when an adapter exceeds the cap rather than truncating", async () => {
    const tool = defineTool(adapter(async () => rows(51)), { maxRows: 50, timeoutMs: 1_000 });
    await expect(tool.invoke({})).rejects.toBeInstanceOf(ToolBoundsViolation);
  });

  it("returns a timeout failure rather than rejecting, and aborts the adapter", async () => {
    const tool = defineTool(
      adapter(
        (_args, _bounds, signal) =>
          new Promise((_resolve, reject) => signal.addEventListener("abort", () => reject(new Error("aborted")))),
      ),
      { maxRows: 10, timeoutMs: 20 },
    );
    const result = await tool.invoke({});
    expect(result).toEqual({ ok: false, failure: { kind: "timeout", timeoutMs: 20 } });
  });

  it("maps a thrown adapter error to a failure value", async () => {
    const tool = defineTool(
      adapter(async () => {
        throw new Error("connection refused");
      }),
      { maxRows: 10, timeoutMs: 1_000 },
    );
    const result = await tool.invoke({});
    expect(result).toEqual({ ok: false, failure: { kind: "backend_error", detail: "connection refused" } });
  });

  it("refuses a tool built without bounds", () => {
    // @ts-expect-error bounds is required, so no caller can register an unbounded tool
    const unbounded = defineTool(adapter(async () => rows(1)));
    expect(unbounded.id).toBe("probe");
  });

  it("refuses a tool that did not come from defineTool", () => {
    const forged = {
      id: "forged",
      description: "",
      parameters: {},
      bounds: { maxRows: 1, timeoutMs: 1 },
      invoke: async () => rows(1),
      close: async () => {},
    };
    // @ts-expect-error the brand is unexported, so only defineTool produces a RegisteredTool
    const registered: RegisteredTool = forged;
    expect(registered.id).toBe("forged");
  });
});

describe("event kinds are closed", () => {
  it("recognises exactly the domain-free set", () => {
    expect(RUN_EVENT_KINDS).toHaveLength(8);
    expect(isRunEventKind("terminal")).toBe(true);
    expect(isRunEventKind("hypothesis")).toBe(false);
  });

  it("discriminates a payload by its kind", () => {
    const event: AgentEvent<{ hypothesis: { statement: string } }> = {
      run_id: START.run_id,
      run_kind: "hunt",
      seq: 1,
      ts: "2026-08-07T00:00:00.000Z",
      kind: "terminal",
      payload: { outcome: "completed", reason: "predicate satisfied" },
      schema_version: 1,
    };
    if (event.kind !== "terminal") throw new Error("unreachable");
    expect(event.payload.outcome).toBe("completed");
    // @ts-expect-error a terminal payload has no statement, whatever the workflow declares
    expect(event.payload.statement).toBeUndefined();
  });
});

describe("a resume job carries nothing but its identity", () => {
  it("dedupes a start on run_id and a resume on ledger position", () => {
    expect(jobIdFor(START)).toBe(START.run_id);
    expect(jobIdFor(RESUME, 7)).toBe(`${START.run_id}:7`);
  });

  it("cannot read a start request", () => {
    if (RESUME.reason !== "resume") throw new Error("unreachable");
    // @ts-expect-error the union has no request on the resume arm, so resume reads the ledger
    expect(RESUME.request).toBeUndefined();
  });
});

describe("an unpriced model is a miss, not a zero", () => {
  it("returns undefined so the budget refuses", () => {
    const table = rateTableOf([
      {
        model_id: "claude-opus-5",
        provider_type: "anthropic",
        input_per_mtok: 5,
        output_per_mtok: 25,
        cache_read_per_mtok: 0.5,
        cache_write_per_mtok: 6.25,
        pricing_source: "exact",
      },
    ]);
    expect(table.lookup("claude-opus-5", "anthropic")?.input_per_mtok).toBe(5);
    expect(table.lookup("claude-opus-5", "openai")).toBeUndefined();
    expect(table.size).toBe(1);
  });
});
