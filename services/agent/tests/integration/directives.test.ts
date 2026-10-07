import { afterAll, beforeEach, describe, expect, it } from "vitest";
import pg from "pg";
import { randomUUID } from "node:crypto";
import { DirectiveRepository } from "../../ledger/directives.js";
import { LedgerRepository } from "../../ledger/repository.js";
import { drain, steer } from "../../workflows/hunt/inbox.js";
import { Journal } from "../../workflows/hunt/journal.js";
import type { HuntKinds } from "../../workflows/hunt/ledger.js";
import type { HuntState } from "../../workflows/hunt/types.js";
import { TEST_DSN } from "../support/testdb.js";

// Two pools rather than one: the point of moving the inbox off a file is that the
// process holding the run is not the process the operator talks to.
const runPool = new pg.Pool({ connectionString: TEST_DSN });
const consolePool = new pg.Pool({ connectionString: TEST_DSN });

const state = new LedgerRepository<HuntKinds>(runPool);
const held = new DirectiveRepository(runPool);
const elsewhere = new DirectiveRepository(consolePool);

afterAll(async () => {
  await runPool.end();
  await consolePool.end();
});

let runId: string;
beforeEach(() => {
  runId = randomUUID();
});

function huntState(): HuntState {
  return {
    hunt_id: `hunt-${runId.slice(0, 8)}`,
    name: "a hunt with an operator watching",
    spec: { name: "test" } as HuntState["spec"],
    seed: runId,
    status: "active",
    outcome: null,
    iteration: 0,
    cost_usd: 0,
    budgets: { max_calls: 4, max_cost_usd: 10, max_wall_ms: 600_000 },
    scope: null,
    narrative: null,
    created_at: new Date().toISOString(),
    terminated_at: null,
    parked_at: null,
    parked_reason: null,
  } as unknown as HuntState;
}

describe("a directive crosses a process boundary to reach its run", () => {
  it("reaches the run when it was queued from another connection entirely", async () => {
    const ledger = await Journal.create(state, held, runId, huntState());

    // The operator's side: a different pool, holding no ledger, knowing only the
    // run id — which is all a console request carries.
    await steer(elsewhere, runId, "note", "the 03:00 spike is our backup window");

    const taken = await drain(ledger);
    await ledger.flush();

    expect(taken.map((directive) => directive.text)).toEqual(["the 03:00 spike is our backup window"]);
    // On the ledger, which is the only thing that makes it true.
    const reopened = await Journal.open(state, held, runId);
    expect(reopened.projection.directives.map((directive) => directive.text)).toEqual([
      "the 03:00 spike is our backup window",
    ]);
  });

  it("re-runs a drain that was interrupted before it flushed, without duplicating", async () => {
    const ledger = await Journal.create(state, held, runId, huntState());
    await steer(elsewhere, runId, "note", "first");
    await steer(elsewhere, runId, "note", "second");

    // Drained but never flushed: the process died between the two, which is the
    // case the queue must not have consumed anything for.
    await drain(ledger);

    const resumed = await Journal.open(state, held, runId);
    expect(resumed.projection.directives).toHaveLength(0);

    const taken = await drain(resumed);
    await resumed.flush();
    expect(taken.map((directive) => directive.text)).toEqual(["first", "second"]);

    // And a second drain after a successful one takes nothing rather than the lot
    // again: what the ledger holds is what the queue excludes.
    const again = await Journal.open(state, held, runId);
    expect(await drain(again)).toHaveLength(0);
    expect(again.projection.directives).toHaveLength(2);
  });

  // Postgres assigns an id at INSERT and reveals the row at COMMIT, so one can appear
  // behind another already journaled. Skipping by count would lose it outright.
  it("journals a directive that only became visible after a later one", async () => {
    const ledger = await Journal.create(state, held, runId, huntState());

    const slow = await consolePool.connect();
    try {
      await slow.query("BEGIN");
      await slow.query(
        `INSERT INTO agent_directives (run_id, directive_id, kind, actor, created_at, payload)
         VALUES ($1, $2, $3, $4, now(), $5)`,
        [
          runId,
          `dir-slow-${runId.slice(0, 8)}`,
          "note",
          "analyst",
          JSON.stringify({
            directive_id: `dir-slow-${runId.slice(0, 8)}`,
            actor: "analyst",
            kind: "note",
            text: "queued first, committed last",
            created_at: new Date().toISOString(),
            origin: "inbox",
          }),
        ],
      );

      // Queued second, committed first, so it holds the higher id.
      await steer(elsewhere, runId, "note", "queued second, committed first");

      const first = await drain(ledger);
      await ledger.flush();
      expect(first.map((directive) => directive.text)).toEqual(["queued second, committed first"]);

      await slow.query("COMMIT");
    } finally {
      slow.release();
    }

    const resumed = await Journal.open(state, held, runId);
    const late = await drain(resumed);
    await resumed.flush();

    expect(late.map((directive) => directive.text)).toEqual(["queued first, committed last"]);
    expect(resumed.projection.directives.map((directive) => directive.text)).toEqual([
      "queued second, committed first",
      "queued first, committed last",
    ]);
  });
});
