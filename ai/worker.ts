import { Worker } from "bullmq";
import pg from "pg";
import { RUN_QUEUE, type RunJob } from "./contracts/job.js";
import type { NewEvent } from "./contracts/events.js";
import { LedgerRepository } from "./ledger/repository.js";

// The walking skeleton (#590): no model call, no tools, no decision vocabulary.
// It exists to prove the seam between the Python backend and this layer.
export async function advance(ledger: LedgerRepository, job: RunJob): Promise<void> {
  const latest = await ledger.latestSeq(job.run_id);
  const events: NewEvent<Record<never, never>>[] = [];

  if (latest === null) {
    if (job.reason !== "start") throw new Error(`cannot resume ${job.run_id}: it has no ledger`);
    events.push({
      run_id: job.run_id,
      run_kind: job.run_kind,
      kind: "run",
      payload: {
        run_kind: job.run_kind,
        spec: job.request,
        budgets: { max_iterations: 0, max_cost_usd: 0 },
        seed: job.run_id,
        tenant_id: job.tenant_id,
        started_by: job.enqueued_by,
      },
    });
  }

  // Re-entrant: a crash between the two appends resumes here rather than
  // colliding on seq 0. The lease and the watchdog are #597, not this slice.
  if ((await ledger.terminal(job.run_id)) === null) {
    events.push({
      run_id: job.run_id,
      run_kind: job.run_kind,
      kind: "terminal",
      payload: { outcome: "completed", reason: "walking skeleton: nothing to decide" },
    });
  }

  await ledger.append(job.run_id, latest === null ? 0 : latest + 1, events);
}

function connectionUrl(): string {
  const url = process.env["DATABASE_URL"];
  if (url === undefined || url === "") throw new Error("DATABASE_URL is not set");
  return url;
}

function redisUrl(): URL {
  return new URL(process.env["REDIS_URL"] ?? "redis://localhost:6379/0");
}

export function startWorker(): Worker<RunJob> {
  const pool = new pg.Pool({ connectionString: connectionUrl() });
  const ledger = new LedgerRepository(pool);
  const url = redisUrl();
  const worker = new Worker<RunJob>(RUN_QUEUE, (job) => advance(ledger, job.data), {
    connection: {
      host: url.hostname,
      port: Number(url.port || 6379),
      db: Number(url.pathname.slice(1) || 0),
      ...(url.password === "" ? {} : { password: url.password }),
    },
  });
  worker.on("closed", () => void pool.end());
  return worker;
}

if (process.argv[1] !== undefined && import.meta.url.endsWith(process.argv[1].split("/").pop() ?? "")) {
  const worker = startWorker();
  process.on("SIGTERM", () => void worker.close());
  process.on("SIGINT", () => void worker.close());
}
