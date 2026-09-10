-- The agent layer's append-only ledger, owned and written only by that layer.
-- The composite primary key is the single-mutator guarantee, not merely an index.

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
    'Append-only ledger owned solely by the agent layer; vigil_app may SELECT and INSERT only. The projection is folded from these rows and never stored.';

COMMENT ON COLUMN agent_events.snapshot IS
    'The digest presented to the lead, selected only by replay and never by the fold.';

COMMENT ON COLUMN agent_events.kind IS
    'Event kind, validated in TypeScript against a closed union and scoped by run_kind.';

-- The one query Python makes beyond an existence check, so it gets an index.
CREATE INDEX IF NOT EXISTS idx_agent_events_terminal
    ON agent_events (run_id) WHERE kind = 'terminal';
