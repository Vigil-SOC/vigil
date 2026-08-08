import { afterAll, beforeEach, describe, expect, it } from "vitest";
import pg from "pg";
import { randomUUID } from "node:crypto";
import { LedgerRepository, SeqConflict } from "../ledger/repository.js";
import type { NewEvent } from "../contracts/events.js";

const pool = new pg.Pool({
  connectionString: process.env["DATABASE_URL"] ?? "postgres://vigil:vigil@localhost:55432/vigil_test",
});
const ledger = new LedgerRepository(pool);

afterAll(() => pool.end());

let runId: string;
beforeEach(() => {
  runId = randomUUID();
});

function runEvent(id: string): NewEvent<Record<never, never>> {
  return {
    run_id: id,
    run_kind: "hunt",
    kind: "run",
    payload: {
      run_kind: "hunt",
      spec: { arch: "arch/threathunt.yaml" },
      budgets: { max_iterations: 0, max_cost_usd: 0 },
      seed: id,
      tenant_id: null,
      started_by: "test",
    },
  };
}

function terminalEvent(id: string): NewEvent<Record<never, never>> {
  return {
    run_id: id,
    run_kind: "hunt",
    kind: "terminal",
    payload: { outcome: "completed", reason: "done" },
  };
}

describe("the ledger is append-only and derives nothing", () => {
  it("assigns seq itself and reads events back in order", async () => {
    const next = await ledger.append(runId, 0, [runEvent(runId), terminalEvent(runId)]);
    expect(next).toBe(2);

    const events = await ledger.read(runId);
    expect(events.map((event) => [event.seq, event.kind])).toEqual([
      [0, "run"],
      [1, "terminal"],
    ]);
    expect(await ledger.latestSeq(runId)).toBe(1);
    expect(await ledger.terminal(runId)).toEqual({ outcome: "completed", reason: "done" });
  });

  it("reports an unknown run as absent rather than empty", async () => {
    expect(await ledger.latestSeq(randomUUID())).toBeNull();
    expect(await ledger.terminal(randomUUID())).toBeNull();
  });

  it("rolls back the whole batch when one event collides", async () => {
    await ledger.append(runId, 0, [runEvent(runId)]);
    await expect(ledger.append(runId, 0, [runEvent(runId), terminalEvent(runId)])).rejects.toBeInstanceOf(SeqConflict);

    const events = await ledger.read(runId);
    expect(events).toHaveLength(1);
  });
});

// The composite primary key is the single-mutator guarantee, not an index:
// this is the test that says so (issue #590).
describe("concurrent writers are rejected by the composite primary key", () => {
  it("lets exactly one of two writers take a ledger position", async () => {
    await ledger.append(runId, 0, [runEvent(runId)]);

    const results = await Promise.allSettled([
      ledger.append(runId, 1, [terminalEvent(runId)]),
      ledger.append(runId, 1, [terminalEvent(runId)]),
    ]);

    const fulfilled = results.filter((r) => r.status === "fulfilled");
    const rejected = results.filter((r) => r.status === "rejected");
    expect(fulfilled).toHaveLength(1);
    expect(rejected).toHaveLength(1);
    expect((rejected[0] as PromiseRejectedResult).reason).toBeInstanceOf(SeqConflict);

    const events = await ledger.read(runId);
    expect(events.filter((event) => event.seq === 1)).toHaveLength(1);
  });

  it("holds under a wider race, with the table still holding one row per seq", async () => {
    await ledger.append(runId, 0, [runEvent(runId)]);

    const attempts = Array.from({ length: 8 }, () => ledger.append(runId, 1, [terminalEvent(runId)]));
    const results = await Promise.allSettled(attempts);

    expect(results.filter((r) => r.status === "fulfilled")).toHaveLength(1);
    for (const result of results.filter((r) => r.status === "rejected")) {
      expect((result as PromiseRejectedResult).reason).toBeInstanceOf(SeqConflict);
    }

    const { rows } = await pool.query<{ count: string }>(
      "SELECT count(*) AS count FROM agent_events WHERE run_id = $1 AND seq = 1",
      [runId],
    );
    expect(rows[0]?.count).toBe("1");
  });
});
