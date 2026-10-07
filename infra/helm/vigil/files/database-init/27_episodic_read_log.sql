-- Every read of episodic memory, whoever asked. Written by core/memory/recall.py
-- inside the query itself, so a read that reached the database is a read that was
-- logged whether it arrived through /internal/tools/invoke or through a direct
-- execute_backend_tool call.
--
-- Not the harness's Ledger recall event, and not a substitute for it. That event
-- exists for replay and lives on the Ledger; this exists for audit, and it is the
-- only thing that makes "we can see what it knew" true for a caller that has no
-- Ledger at all -- an evaluation harness, a console query, a Case-side reader.
--
-- It holds reads rather than facts, which is why it is the one part of the tier
-- with a retention policy: the Sightings and Verdicts a read returned are still
-- in their own tables, so an expired log row loses the fact that someone asked
-- and never the answer they were given. Retained for the daemon's
-- scheduler.cleanup_retention_days, 90 days by default.

CREATE TABLE IF NOT EXISTS episodic_read_log (
    id          bigserial   PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    -- Deliberately open text and not a CHECK, unlike every domain in
    -- 26_episodic_memory.sql: the callers are a hunt worker, an evaluation
    -- harness, a Case-side reader and whatever asks next, and a closed list here
    -- would turn a new kind of caller into a failed read rather than a logged
    -- one. Defaulted at 'unknown' because an unattributed read is still worth
    -- logging; a log that refuses what it cannot attribute is a log of the
    -- well-behaved callers only.
    caller_kind text        NOT NULL DEFAULT 'unknown',
    caller_id   text        NOT NULL DEFAULT 'unknown',
    -- As queried, not as asked for: normalised by core/memory/entity_keys.py, so
    -- a row here can be compared against a stored key without re-deriving it.
    keys        text[]      NOT NULL,
    -- The freshness filter that ran. Distinct from ts, because the Distil polls:
    -- an investigation that ended Monday can be written Wednesday carrying
    -- Monday's date, inside this predicate and absent from a read that ran on
    -- Tuesday. Recording as_of is what lets a later reader tell those apart.
    as_of       timestamptz NOT NULL,
    -- Counts and not the rows. Copying the rows would double the tier's storage
    -- to say what the tier already says, and would freeze a copy that the
    -- Distil's delete-then-insert may since have superseded.
    row_counts  jsonb       NOT NULL,
    -- What the caller was not shown, per kind and per reason. A caller given a
    -- partial view needs to know it was partial, and this is the record that it
    -- was told.
    dropped     jsonb       NOT NULL,
    -- The parameters that chose the set, copied in rather than referenced: the
    -- caps are constants in the reader and a later release may change them, at
    -- which point a row without them cannot be read back.
    ranking     jsonb       NOT NULL
);

COMMENT ON TABLE episodic_read_log IS
    'One row per read of episodic memory, for audit rather than replay. Retention is the daemon''s cleanup sweep, which deletes rows older than scheduler.cleanup_retention_days -- 90 days unless a deployment says otherwise.';

COMMENT ON COLUMN episodic_read_log.keys IS
    'The Entity Keys as queried -- normalised -- rather than as the caller spelled them.';

COMMENT ON COLUMN episodic_read_log.as_of IS
    'The freshness filter the read ran with, which is not the time the read happened.';

-- The cleanup sweep's only query, and the ordering an audit reads in.
CREATE INDEX IF NOT EXISTS idx_episodic_read_log_ts
    ON episodic_read_log (ts);

-- "What has anyone asked about this entity", which is the audit question that
-- does not scan.
CREATE INDEX IF NOT EXISTS idx_episodic_read_log_keys
    ON episodic_read_log USING GIN (keys);
