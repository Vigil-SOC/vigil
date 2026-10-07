import { afterAll, afterEach, beforeEach, describe, expect, it } from "vitest";
import { randomUUID } from "node:crypto";
import pg from "pg";
import { LeaseRepository } from "../../ledger/leases.js";
import { TEST_DSN } from "../support/testdb.js";

// Two pools, because the claim is about two processes rather than two objects: one
// connection proves nothing about the statement that refuses the second worker.
const first = new pg.Pool({ connectionString: TEST_DSN });
const second = new pg.Pool({ connectionString: TEST_DSN });

const workerA = new LeaseRepository(first);
const workerB = new LeaseRepository(second);

afterAll(async () => {
  await Promise.all([first.end(), second.end()]);
});

let runId: string;
beforeEach(() => {
  runId = randomUUID();
});

// Rows outlive a test by design -- presence means unfinished -- so this file tidies
// up after itself rather than letting the table grow across runs.
afterEach(async () => {
  await first.query("DELETE FROM agent_run_leases WHERE run_id = $1", [runId]);
});

// Sorted to the front of the sweep: other suites hold rows here too, and a sweep is
// ordered by claim_until with a limit, so this run has to be the most overdue.
async function isDue(): Promise<boolean> {
  const row = await first.query<{ due: boolean }>(
    "SELECT claim_until <= now() AS due FROM agent_run_leases WHERE run_id = $1",
    [runId],
  );
  return row.rows[0]?.due ?? false;
}

async function overdue(): Promise<void> {
  await first.query("UPDATE agent_run_leases SET claim_until = now() - interval '1 year' WHERE run_id = $1", [runId]);
}

describe("two workers cannot hold one run", () => {
  it("refuses the second claim while the first is live", async () => {
    expect(await workerA.claim(runId, "hunt", "a", 60_000)).toBe(true);
    expect(await workerB.claim(runId, "hunt", "b", 60_000)).toBe(false);
  });

  // Under contention rather than in sequence: both statements race for the same
  // row, and ON CONFLICT is what makes exactly one of them win.
  it("lets exactly one of two simultaneous claims through", async () => {
    const outcomes = await Promise.all([
      workerA.claim(runId, "hunt", "a", 60_000),
      workerB.claim(runId, "hunt", "b", 60_000),
    ]);

    expect(outcomes.filter((won) => won)).toHaveLength(1);
  });

  it("holds under a wider race, with one holder at the end of it", async () => {
    const claims = Array.from({ length: 12 }, (_, index) =>
      (index % 2 === 0 ? workerA : workerB).claim(runId, "hunt", `worker-${index}`, 60_000),
    );

    expect((await Promise.all(claims)).filter((won) => won)).toHaveLength(1);

    const held = await first.query<{ owner: string }>("SELECT owner FROM agent_run_leases WHERE run_id = $1", [runId]);
    expect(held.rows).toHaveLength(1);
  });

  // A lapsed claim is what tells a watchdog the worker is gone, so it must be
  // takeable -- and the displaced holder must be able to find out it was taken.
  it("lets a lapsed claim be taken, and tells the old holder on its next renewal", async () => {
    await workerA.claim(runId, "hunt", "a", 0);

    expect(await workerB.claim(runId, "hunt", "b", 60_000)).toBe(true);
    expect(await workerA.renew(runId, "a", 60_000)).toBe(false);
    expect(await workerB.renew(runId, "b", 60_000)).toBe(true);
  });

  // Renewal keys on the owner, not the expiry: nobody took the run, so a late
  // renewal still holds it. Event-loop starvation must not kill a run.
  it("lets a late renewal succeed while nobody else has claimed the run", async () => {
    await workerA.claim(runId, "hunt", "a", 0);

    expect(await workerA.renew(runId, "a", 60_000)).toBe(true);
  });
});

describe("the sweeper claims in the same statement it discovers by", () => {
  it("hands a due run to exactly one of two sweepers", async () => {
    await workerA.claim(runId, "hunt", "a", 0);
    await overdue();

    const swept = await Promise.all([workerA.sweep(60_000, 10), workerB.sweep(60_000, 10)]);
    const mine = swept.flat().filter((claim) => claim.run_id === runId);

    expect(mine).toHaveLength(1);
    expect(mine[0]?.run_kind).toBe("hunt");
  });

  it("does not offer a run whose claim is still live", async () => {
    await workerA.claim(runId, "hunt", "a", 60_000);

    const swept = await workerB.sweep(60_000, 50);

    expect(swept.map((claim) => claim.run_id)).not.toContain(runId);
  });

  // A finished run is nobody's work, and its absence rather than a status column is
  // what says so -- nothing here can disagree with the ledger.
  it("stops offering a run once it is finished", async () => {
    await workerA.claim(runId, "hunt", "a", 0);
    await workerA.finish(runId);

    const swept = await workerB.sweep(60_000, 50);

    expect(swept.map((claim) => claim.run_id)).not.toContain(runId);
  });

  // What makes the watchdog work: the sweeper reserves the row and the worker across
  // the queue takes it. A reservation in the sweeper's name refuses that worker.
  it("lets the worker that picks the job up claim a reserved run", async () => {
    await workerA.claim(runId, "hunt", "a", 0);
    await overdue();
    expect((await workerA.sweep(60_000, 50)).map((claim) => claim.run_id)).toContain(runId);

    expect(await workerB.claim(runId, "hunt", "b", 60_000)).toBe(true);
  });

  // A parked run is the same state, nobody driving it, so an answer takes effect
  // now rather than waiting out an interval nobody is using.
  it("lets a worker claim a parked run before its interval passes", async () => {
    await workerA.claim(runId, "hunt", "a", 60_000);
    await workerA.release(runId, "a", 60_000);

    expect(await workerB.claim(runId, "hunt", "b", 60_000)).toBe(true);
  });

  // A parked run keeps its row: it is unfinished, so the sweeper comes back to it
  // when the interval passes, which is also what its park TTL is measured against.
  it("keeps a released run on the list but not due before its interval", async () => {
    await workerA.claim(runId, "hunt", "a", 60_000);
    await workerA.release(runId, "a", 60_000);

    expect((await workerB.sweep(60_000, 50)).map((claim) => claim.run_id)).not.toContain(runId);
  });

  // What the console does when an answer arrives. Asserted on the column, not through
  // a sweep: a sweep is ordered against other suites' rows and could pass by accident.
  it("makes a woken run due at once", async () => {
    await workerA.claim(runId, "hunt", "a", 60_000);
    await workerA.release(runId, "a", 60_000);
    expect(await isDue()).toBe(false);

    await workerB.wake(runId);

    expect(await isDue()).toBe(true);
  });

  // Waking a run somebody is working on would declare that worker dead.
  it("leaves a held run alone when woken", async () => {
    await workerA.claim(runId, "hunt", "a", 60_000);

    await workerB.wake(runId);

    expect(await isDue()).toBe(false);
  });
});
