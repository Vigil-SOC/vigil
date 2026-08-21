import { afterAll, beforeEach, describe, expect, it } from "vitest";
import pg from "pg";
import { randomUUID } from "node:crypto";
import { join } from "node:path";
import { LedgerRepository } from "../../ledger/repository.js";
import { LeaseRepository } from "../../ledger/leases.js";
import { advance, resolveSpec } from "../../worker.js";
import { runHunt } from "../../workflows/hunt/workflow.js";
import { archFor } from "../../arch/registry.js";
import { InProcessDirectiveQueue } from "../../workflows/hunt/directives.js";
import type { RunJob } from "../../contracts/job.js";
import { isLead, respondingProvider } from "../support/responding-provider.js";
import { budgetOf, FRESH, unmeteredQuota } from "../../core/budget.js";
import { localDispatch } from "../../core/dispatch.js";
import { nullMemory } from "../../core/memory.js";
import { registryOf } from "../../core/registry.js";
import type { HarnessFactory } from "../../harness.js";

// A hunt started through the queue must reach the hunt loop. Routing it to the
// generic one produced a run that completed and looked fine, and hunted nothing.
const FIXTURES = join(import.meta.dirname, "..", "fixtures");

const pool = new pg.Pool({
  connectionString: process.env["DATABASE_URL"] ?? "postgres://vigil:vigil@localhost:55432/vigil_test",
});
const ledger = new LedgerRepository(pool);
const leases = new LeaseRepository(pool);

afterAll(() => pool.end());

let runId: string;
beforeEach(() => {
  runId = randomUUID();
});

function startJob(id: string): RunJob {
  return {
    schema_version: 1,
    run_id: id,
    run_kind: "hunt",
    tenant_id: null,
    enqueued_at: new Date().toISOString(),
    enqueued_by: "test",
    reason: "start",
    request: {
      arch: "",
      playbook: join(FIXTURES, "hunt.playbook.yaml"),
      config: join(FIXTURES, "hunt.config.yaml"),
      prompt: "go",
    },
  };
}

// Answers by role rather than position: one iteration asks the lead, the workers
// and the critic, which a positional script cannot keep up with.
function huntHarness(): HarnessFactory {
  const provider = respondingProvider({
    emit: (schema) =>
      isLead(schema)
        ? { action: "CONCLUDE", rationale: "nothing to pursue", evidence_citations: [] }
        : { results: [] },
    ticks: 0,
  });
  return (_kind, spec, state, memory = nullMemory, seed = FRESH) => ({
    provider,
    registry: registryOf([], {}),
    dispatch: localDispatch,
    budget: budgetOf(spec.budgets, unmeteredQuota, Date.now, seed),
    memory,
    state,
  });
}

// A pool with nothing left, so the first beginCall refuses and no model is reached.
function spentHarness(): HarnessFactory {
  const base = huntHarness();
  return (kind, spec, state, memory, seed) => ({
    ...base(kind, spec, state, memory, seed),
    budget: budgetOf({ ...spec.budgets, max_calls: 0 }, unmeteredQuota),
  });
}

async function run(id: string, build: HarnessFactory = huntHarness()): Promise<void> {
  await advance(ledger, leases, startJob(id), build, new InProcessDirectiveQueue());
}

describe("a hunt started through the queue", () => {
  it("opens a ledger carrying the hunt's own state, not a generic run", async () => {
    await run(runId);

    const [first] = await ledger.read(runId);
    expect(first?.kind).toBe("run");
    // Both halves: the contract's spec, which resume reads, and the hunt state
    // its projection folds. A run event with only one of them breaks the other.
    expect(first?.payload).toMatchObject({
      spec: { arch: "threathunt" },
      started_by: "test",
      hunt: { name: expect.any(String), status: expect.any(String) },
    });
  });

  it("journals the hunt's vocabulary rather than the lead loop's", async () => {
    await run(runId);

    const kinds = new Set((await ledger.read(runId)).map((event) => String(event.kind)));
    // finalize is the hunt's report and nothing else writes one, so it is what
    // tells a hunt apart from a run the generic lead loop drove to the same end.
    expect(kinds.has("finalize")).toBe(true);
    // And the run is over for everyone, not only for the hunt's own projection:
    // the lease, the API's status and the sweeper all read the domain-free kind.
    expect(kinds.has("terminal")).toBe(true);
  });

  it("reaches a terminal the ledger can report", async () => {
    await run(runId);

    const terminal = await ledger.terminal(runId);
    expect(terminal).not.toBeNull();
    expect((await ledger.read(runId)).at(-1)?.kind).toBe("terminal");
  });

  // Only compose was handed a mirror, so a finished hunt never reported its
  // outcome and the console's workflow_runs row stayed running forever.
  it("reports its terminal to the backend, not only to the ledger", async () => {
    const posted: { url: string; body: unknown }[] = [];
    const real = globalThis.fetch;
    process.env["VIGIL_RUNS_URL"] = "http://backend/internal/runs";
    // Setting the variable turns the answers reader on too, and that one GETs.
    globalThis.fetch = (async (url: string, init?: RequestInit) => {
      if (init?.body !== undefined) posted.push({ url: String(url), body: JSON.parse(String(init.body)) });
      return { ok: true, status: 200, json: async () => ({ decisions: [] }) } as Response;
    }) as unknown as typeof globalThis.fetch;

    try {
      await run(runId);
    } finally {
      globalThis.fetch = real;
      delete process.env["VIGIL_RUNS_URL"];
    }

    const terminal = posted.find((one) => one.url.endsWith(`${runId}/terminal`));
    expect(terminal?.body).toMatchObject({ outcome: "completed" });
  });

  // The report and the spend were on the ledger and nowhere else, so the console
  // showed a finished hunt with a blank summary and a dash where its dollars go.
  it("reports the hunt's own report and what it cost, not only that it ended", async () => {
    await run(runId);

    const terminal = await ledger.terminal(runId);
    expect(terminal?.summary).toContain("#");
    expect(terminal?.handoffs).toEqual([]);
  });

  // Compose, lead and tally all end on outcome.refusal; the hunt threw instead, so
  // a run that spent its allowance stayed "running" forever and the watchdog
  // re-enqueued it every sweep to be refused again. It parks rather than
  // terminating: the harness ceiling that stops a hunt is usually the wall clock,
  // which stops it with turns and dollars still on the board, and parked is the
  // state extend/conclude/abort are answered from. A re-enqueue is cheap now --
  // advanceIteration throws on the parked status before any call is made.
  it("parks a run that spent its allowance rather than leaving it running", async () => {
    await run(runId, spentHarness());

    expect(await ledger.terminal(runId)).toBeNull();
    const events = await ledger.read(runId);
    const parked = events.filter(
      (event) => event.kind === "patch" && (event.payload as { fields?: { status?: string } }).fields?.status === "parked",
    );
    expect(parked).toHaveLength(1);
  });

  // The signal fires for exactly one reason: renewal found another worker holding
  // the lease. Writing a terminal here would end the run that worker is driving.
  it("hands a run back on a lost lease rather than ending it", async () => {
    const job = startJob(runId);
    const spec = await resolveSpec(job as Extract<typeof job, { reason: "start" }>);
    const halted = AbortSignal.abort();
    const build = huntHarness();

    const outcome = await runHunt(build("hunt", spec, ledger, undefined, undefined) as never, {
      run_id: runId,
      spec,
      actions: archFor("hunt").actions,
      queue: new InProcessDirectiveQueue(),
      started_by: "test",
      signal: halted,
    });

    expect(outcome.status).toBe("aborted");
    // The run is still open, so the worker that took the lease can carry it on.
    expect(await ledger.terminal(runId)).toBeNull();
  });

  // The run event is written once. A second attempt on a settled run must not
  // re-open it -- an empty script proves nothing reached the model at all.
  it("does not re-open a hunt that already reached terminal", async () => {
    await run(runId);
    const settled = (await ledger.read(runId)).length;

    const refusing: HarnessFactory = () => {
      throw new Error("a settled run must not reach a workflow at all");
    };
    await advance(ledger, leases, startJob(runId), refusing, new InProcessDirectiveQueue());

    expect(await ledger.read(runId)).toHaveLength(settled);
  });
});
