import { describe, expect, it } from "vitest";
import { InProcessState } from "../../core/state.js";
import { SpecError } from "../../core/spec.js";
import { toolsFrom } from "../../tools/remote.js";
import { expandFrom } from "../../plugins/hunt/expand.js";
import type { HuntKinds } from "../../plugins/hunt/ledger.js";
import type { NewEvent } from "../../contracts/events.js";

const RUN = "3c1c2d3e-0000-4000-8000-000000000700";

const SPEC = {
  id: "expand",
  kind: "local",
  description: "return the raw payloads behind evidence ids",
  parameters: { type: "object", properties: { evidence_ids: { type: "array" } } },
};

function state(): InProcessState<HuntKinds> {
  return new InProcessState<HuntKinds>();
}

// The fold refuses a ledger that does not open with a run event, so a store
// standing in for one has to open the same way a real hunt does.
async function opened(store: InProcessState<HuntKinds>): Promise<void> {
  await store.append(RUN, [
    {
      run_id: RUN,
      run_kind: "hunt",
      kind: "run",
      payload: { hunt: { hunt_id: "hunt-1", name: "t", status: "active", budgets: {}, scope: {}, iteration: 0, cost_usd: 0 } },
    } as unknown as NewEvent<HuntKinds>,
  ]);
}

async function withEvidence(store: InProcessState<HuntKinds>, id: string, payload: unknown): Promise<void> {
  await opened(store);
  await store.append(RUN, [
    {
      run_id: RUN,
      run_kind: "hunt",
      kind: "evidence",
      payload: { evidence_id: id, payload, source_system: "test", summary: "s", salience: "routine", why_notable: "w" },
    } as unknown as NewEvent<HuntKinds>,
  ]);
}

// A tool whose answer is the run's own record cannot be served by the backend,
// which has never seen the ledger. Declared local, and resolved in this process.
describe("declaring a tool local", () => {
  it("registers it when the run supplies an implementation", () => {
    const [tool] = toolsFrom([SPEC], { expand: expandFrom(state(), RUN) });
    expect(tool?.id).toBe("expand");
  });

  // harnessFor builds a registry before the run is known, so it supplies no
  // locals: the tool registers and refuses at call time, and the workflow that
  // owns it replaces this one once it has the run in hand.
  it("registers a local nothing supplied, and fails it at call time", async () => {
    const [tool] = toolsFrom([SPEC], {});
    expect(tool?.id).toBe("expand");
    const result = await tool!.invoke({ evidence_ids: ["e1"] });
    expect(result.ok).toBe(false);
  });

  it("still refuses a kind nothing implements", () => {
    expect(() => toolsFrom([{ ...SPEC, kind: "duckdb" }], {})).toThrow(SpecError);
  });
});

describe("expanding a record the lead was shown", () => {
  it("returns the raw payload behind an id", async () => {
    const store = state();
    await withEvidence(store, "ev-1", { bytes_out: 4096, dest: "45.77.53.176" });

    const result = await expandFrom(store, RUN)({ evidence_ids: ["ev-1"] }, { maxRows: 10, timeoutMs: 500 }, new AbortController().signal);

    expect(result.ok).toBe(true);
    const row = result.ok ? (result.rows[0] as Record<string, unknown>) : {};
    expect(row["expanded"]).toBe(true);
    expect(String(row["payload"])).toContain("45.77.53.176");
  });

  // Named rather than skipped: an id the ledger does not hold means the lead
  // cited something it was never shown, which it should learn.
  it("says so when an id is on no record", async () => {
    const store = state();
    await opened(store);
    const result = await expandFrom(store, RUN)({ evidence_ids: ["ev-missing"] }, { maxRows: 10, timeoutMs: 500 }, new AbortController().signal);

    const row = result.ok ? (result.rows[0] as Record<string, unknown>) : {};
    expect(row["expanded"]).toBe(false);
    expect(String(row["reason"])).toContain("no record");
  });

  // Whole records are dropped at the budget rather than one cut mid-JSON, which
  // is the rule the EXPAND action already applies.
  it("drops a record too large to expand rather than truncating it", async () => {
    const store = state();
    await withEvidence(store, "ev-big", { blob: "x".repeat(20_000) });

    const result = await expandFrom(store, RUN)({ evidence_ids: ["ev-big"] }, { maxRows: 10, timeoutMs: 500 }, new AbortController().signal);

    const row = result.ok ? (result.rows[0] as Record<string, unknown>) : {};
    expect(row["expanded"]).toBe(false);
    expect(String(row["reason"])).toContain("too large");
  });

  it("reports a call that named nothing as a defect in the call", async () => {
    const result = await expandFrom(state(), RUN)({}, { maxRows: 10, timeoutMs: 500 }, new AbortController().signal);
    expect(result.ok === false && result.failure.kind).toBe("invalid_args");
  });
});
