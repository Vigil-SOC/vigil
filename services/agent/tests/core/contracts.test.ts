import { describe, expect, it } from "vitest";
import {
  defineTool,
  ToolBoundsViolation,
  type RegisteredTool,
  type ToolAdapter,
  type ToolResult,
} from "../../contracts/tool.js";
import { RUN_EVENT_KINDS, isRunEventKind, type AgentEvent } from "../../contracts/events.js";
import { jobIdFor, type RunJob } from "../../contracts/job.js";

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
    expect(RUN_EVENT_KINDS).toHaveLength(10);
    expect(isRunEventKind("terminal")).toBe(true);
    // Domain-free because every run kind recalls, not because a hunt does.
    expect(isRunEventKind("recall")).toBe(true);
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
  // A start dedupes so a double POST cannot open two runs; a resume deliberately
  // does not, or the queue would drop every check after the first.
  it("dedupes a start on run_id and gives every resume its own id", () => {
    expect(jobIdFor(START, "unused")).toBe(START.run_id);
    // No colon: BullMQ refuses a custom id holding one, and the sweeper is the
    // only caller, so the throw would take every resume with it.
    expect(jobIdFor(RESUME, "a1b2")).toBe(`${START.run_id}-a1b2`);
    expect(jobIdFor(RESUME, "a1b2")).not.toContain(":");
    expect(jobIdFor(RESUME, "c3d4")).not.toBe(jobIdFor(RESUME, "a1b2"));
  });

  it("cannot read a start request", () => {
    if (RESUME.reason !== "resume") throw new Error("unreachable");
    // @ts-expect-error the union has no request on the resume arm, so resume reads the ledger
    expect(RESUME.request).toBeUndefined();
  });
});

