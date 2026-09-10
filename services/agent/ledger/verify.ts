import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import pg, { type Pool } from "pg";
import { poolConfig } from "../core/db.js";

export type ChainOk = { ok: true; events: number; runs: number };
export type ChainBreak = {
  ok: false;
  runId: string;
  seq: number;
  reason: "prev_hash" | "payload";
};
export type VerifyResult = ChainOk | ChainBreak;

const USAGE = "usage: ledger verify [--run-id <uuid>]";

const FIRST_BREAK = `
SELECT run_id, seq,
       CASE
         WHEN prev_hash IS DISTINCT FROM expected_prev THEN 'prev_hash'
         ELSE 'payload'
       END AS reason
  FROM (
    SELECT run_id, seq, prev_hash, event_hash,
           coalesce(lag(event_hash) OVER (PARTITION BY run_id ORDER BY seq), '') AS expected_prev,
           agent_event_hash(prev_hash, payload) AS expected_hash
      FROM agent_events
     WHERE ($1::uuid IS NULL OR run_id = $1)
  ) c
 WHERE prev_hash IS DISTINCT FROM expected_prev
    OR event_hash IS DISTINCT FROM expected_hash
 ORDER BY run_id, seq
 LIMIT 1`;

const COUNTS = `
SELECT count(*)::int AS events, count(DISTINCT run_id)::int AS runs
  FROM agent_events
 WHERE ($1::uuid IS NULL OR run_id = $1)`;

// Walks stored hashes against the SQL formula the INSERT trigger assigned.
export async function verifyLedger(pool: Pool, runId?: string): Promise<VerifyResult> {
  const key = runId ?? null;
  const broken = await pool.query<{ run_id: string; seq: number; reason: string }>(FIRST_BREAK, [key]);
  const row = broken.rows[0];
  if (row !== undefined) {
    return { ok: false, runId: String(row.run_id), seq: Number(row.seq), reason: asReason(row.reason) };
  }
  const counts = await pool.query<{ events: number; runs: number }>(COUNTS, [key]);
  const totals = counts.rows[0];
  return { ok: true, events: Number(totals?.events ?? 0), runs: Number(totals?.runs ?? 0) };
}

function asReason(value: string): ChainBreak["reason"] {
  switch (value) {
    case "prev_hash":
    case "payload":
      return value;
    default:
      throw new Error(`unexpected break reason: ${value}`);
  }
}

export function formatVerify(result: VerifyResult): string {
  if (result.ok) return `ok  ${result.events} events  ${result.runs} run(s)`;
  switch (result.reason) {
    case "prev_hash":
      return `break  run=${result.runId}  seq=${result.seq}  prev_hash does not follow the previous event`;
    case "payload":
      return `break  run=${result.runId}  seq=${result.seq}  payload does not match event_hash`;
    default: {
      const _exhaustive: never = result.reason;
      return _exhaustive;
    }
  }
}

export function parseRunId(argv: readonly string[]): string | undefined {
  let runId: string | undefined;
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === undefined) throw new Error(USAGE);
    if (arg === "--run-id") {
      const value = argv[i + 1];
      if (value === undefined || value === "" || value.startsWith("-")) throw new Error(USAGE);
      runId = value;
      i += 1;
      continue;
    }
    if (arg.startsWith("--run-id=")) {
      const value = arg.slice("--run-id=".length);
      if (value === "") throw new Error(USAGE);
      runId = value;
      continue;
    }
    throw new Error(USAGE);
  }
  return runId;
}

export async function main(argv: readonly string[] = process.argv.slice(2)): Promise<number> {
  let runId: string | undefined;
  try {
    runId = parseRunId(argv);
  } catch (error) {
    process.stderr.write(`${error instanceof Error ? error.message : USAGE}\n`);
    return 2;
  }
  const pool = new pg.Pool(poolConfig());
  try {
    const result = await verifyLedger(pool, runId);
    process.stdout.write(`${formatVerify(result)}\n`);
    return result.ok ? 0 : 1;
  } finally {
    await pool.end();
  }
}

const invoked = process.argv[1];
if (invoked !== undefined && import.meta.url === pathToFileURL(resolve(invoked)).href) {
  main().then((code) => process.exit(code), (error: unknown) => {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exit(2);
  });
}
