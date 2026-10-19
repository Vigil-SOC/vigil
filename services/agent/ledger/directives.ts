import type { Pool } from "pg";
import type { DirectiveQueue } from "../plugins/hunt/ports.js";
import type { Directive } from "../plugins/hunt/types.js";

// The operator's queue in Postgres. It writes agent_directives and never
// agent_events: enqueuing is open to any process, journaling is not.
export class DirectiveRepository implements DirectiveQueue {
  constructor(private readonly pool: Pool) {}

  // Idempotent on directive_id, so a retried enqueue is not a second directive.
  // The row carries the whole thing; the columns answer who steered a run, and when.
  async enqueue(runId: string, directive: Directive): Promise<void> {
    await this.pool.query(
      `INSERT INTO agent_directives (run_id, directive_id, kind, actor, created_at, payload)
       VALUES ($1, $2, $3, $4, $5, $6)
       ON CONFLICT (directive_id) DO NOTHING`,
      [
        runId,
        directive.directive_id,
        directive.kind,
        directive.actor,
        directive.created_at,
        JSON.stringify(directive),
      ],
    );
  }

  // Insertion order, because that is the order an operator meant them in. Excluding
  // by id rather than count survives one committing behind another already journaled.
  async pending(runId: string, journaled: readonly string[]): Promise<Directive[]> {
    const result = await this.pool.query<{ directive_id: string; payload: Directive }>(
      `SELECT directive_id, payload FROM agent_directives
       WHERE run_id = $1 AND directive_id <> ALL($2::text[])
       ORDER BY id`,
      [runId, journaled],
    );
    // Identity comes from the guarded column, not the payload: a hand-written row
    // could disagree with itself, and the drain excludes on this id.
    return result.rows.map((row) => ({ ...row.payload, directive_id: row.directive_id }));
  }
}
