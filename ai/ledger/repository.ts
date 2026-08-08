import type { Pool, PoolClient } from "pg";
import {
  EVENT_SCHEMA_VERSION,
  type AgentEvent,
  type NewEvent,
  type RunKind,
  type TerminalPayload,
} from "../contracts/events.js";

// A second writer reached the same ledger position. Not an error to retry
// blindly: the run advanced underneath this worker, so it must re-read first.
export class SeqConflict extends Error {
  constructor(readonly runId: string, readonly seq: number) {
    super(`ledger position ${runId}/${seq} is already taken`);
  }
}

const UNIQUE_VIOLATION = "23505";

// The single writer to agent_events (ADR-0001). Assigns seq itself, so no
// caller chooses its own position in the log.
export class LedgerRepository<Kinds extends Record<string, unknown> = Record<never, never>> {
  constructor(private readonly pool: Pool) {}

  async latestSeq(runId: string): Promise<number | null> {
    const result = await this.pool.query<{ max: number | null }>(
      "SELECT MAX(seq) AS max FROM agent_events WHERE run_id = $1",
      [runId],
    );
    return result.rows[0]?.max ?? null;
  }

  async read(runId: string): Promise<AgentEvent<Kinds>[]> {
    const result = await this.pool.query(
      "SELECT run_id, run_kind, seq, ts, kind, payload, snapshot, schema_version FROM agent_events WHERE run_id = $1 ORDER BY seq",
      [runId],
    );
    return result.rows.map(rowToEvent) as AgentEvent<Kinds>[];
  }

  // One transaction, so a partially written iteration never lands. The events
  // are numbered from `from`, which the caller read before deciding to append.
  async append(runId: string, from: number, events: readonly NewEvent<Kinds>[]): Promise<number> {
    if (events.length === 0) return from;
    const client = await this.pool.connect();
    try {
      await client.query("BEGIN");
      let seq = from;
      for (const event of events) await insert(client, event, seq++);
      await client.query("COMMIT");
      return seq;
    } catch (error) {
      await client.query("ROLLBACK");
      throw isUniqueViolation(error) ? new SeqConflict(runId, from) : error;
    } finally {
      client.release();
    }
  }

  // One of the two queries Python is permitted against this table; it lives
  // here too so the worker and the API agree on what terminal means.
  async terminal(runId: string): Promise<TerminalPayload | null> {
    const result = await this.pool.query<{ payload: TerminalPayload }>(
      "SELECT payload FROM agent_events WHERE run_id = $1 AND kind = 'terminal' ORDER BY seq LIMIT 1",
      [runId],
    );
    return result.rows[0]?.payload ?? null;
  }
}

// Structural rather than NewEvent<Kinds>: the insert reads the envelope only,
// and never needs to know which workflow's payload union it is holding.
interface Insertable {
  run_id: string;
  run_kind: RunKind;
  kind: string;
  payload: unknown;
  snapshot?: unknown;
}

async function insert(client: PoolClient, event: Insertable, seq: number): Promise<void> {
  await client.query(
    "INSERT INTO agent_events (run_id, run_kind, seq, kind, payload, snapshot, schema_version) VALUES ($1, $2, $3, $4, $5, $6, $7)",
    [
      event.run_id,
      event.run_kind,
      seq,
      event.kind,
      JSON.stringify(event.payload),
      event.snapshot === undefined ? null : JSON.stringify(event.snapshot),
      EVENT_SCHEMA_VERSION,
    ],
  );
}

function rowToEvent(row: Record<string, unknown>): AgentEvent<Record<never, never>> {
  return {
    run_id: String(row["run_id"]),
    run_kind: row["run_kind"] as RunKind,
    seq: Number(row["seq"]),
    ts: (row["ts"] as Date).toISOString(),
    kind: String(row["kind"]),
    payload: row["payload"],
    ...(row["snapshot"] === null ? {} : { snapshot: row["snapshot"] }),
    schema_version: Number(row["schema_version"]),
  } as AgentEvent<Record<never, never>>;
}

function isUniqueViolation(error: unknown): boolean {
  return typeof error === "object" && error !== null && (error as { code?: string }).code === UNIQUE_VIOLATION;
}
