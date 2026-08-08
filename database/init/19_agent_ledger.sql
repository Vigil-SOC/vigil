-- The agent layer's append-only ledger (GH #589, #590; ADR-0001, ADR-0002)
-- The TypeScript agent layer owns this table and is its single writer. It is
-- the only durable state a run has: the projection, the report and replay are
-- all folds over these rows and are never separately persisted.
--
-- The composite primary key IS the single-mutator guarantee, not merely an
-- index -- a concurrent second writer gets a unique violation rather than
-- silently interleaving. The per-run lease (GH #597) is for liveness, so a
-- crashed holder does not block a run forever; it is not what makes this safe.
--
-- run_kind carries which workflow produced the row, so one table serves hunt,
-- investigate and compose. kind is validated in TypeScript against a closed
-- union rather than by a CHECK: adding an event kind must never be a migration.
--
-- Idempotent, and issues no DDL from the agent layer itself (migration-plan D10).

CREATE TABLE IF NOT EXISTS agent_events (
    run_id         uuid        NOT NULL,
    seq            integer     NOT NULL,
    ts             timestamptz NOT NULL DEFAULT now(),
    run_kind       text        NOT NULL,
    kind           text        NOT NULL,
    payload        jsonb       NOT NULL,
    snapshot       jsonb,
    schema_version integer     NOT NULL,
    PRIMARY KEY (run_id, seq)
);

COMMENT ON TABLE agent_events IS
    'Append-only event ledger owned solely by the TypeScript agent layer. The '
    'projection is folded from these rows and is never stored. Python reads only '
    'run existence and the terminal event; any richer read means a second fold.';

COMMENT ON COLUMN agent_events.snapshot IS
    'The digest presented to the lead. Selected only by replay, never by the '
    'fold: decision events reach 56.7 KB, so folding a long run would otherwise '
    'read tens of megabytes to build one digest.';

COMMENT ON COLUMN agent_events.kind IS
    'Event kind, validated in TypeScript against a closed union. Domain-free '
    'kinds (run, spend, dispatch, checkpoint, resolution, directive, patch, '
    'terminal) are shared; the rest belong to the workflow named by run_kind.';

-- Reporting a run's outcome is the one query the Python API makes against this
-- table beyond an existence check, so it gets the index rather than a scan.
CREATE INDEX IF NOT EXISTS idx_agent_events_terminal
    ON agent_events (run_id) WHERE kind = 'terminal';
