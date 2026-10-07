-- Distil failure state (#734): a run the Distil could not write is a row
-- someone can query.
--
-- #731 made a failed write loud -- it raises, is retried inside the tick, and
-- is reported at ERROR with a traceback. What it left absent is state. A
-- failure writes no marker, deliberately, so that the poll offers the run
-- again; the cost is that a run that failed and a run nothing has reached yet
-- are the same absence, and a run failing permanently is retried on every tick
-- forever with nothing but the log to say so.
--
-- One table for both Distils, keyed by what the poll actually holds before it
-- has anything else. Not columns on episodic_distil_markers: that table is
-- keyed by investigation, and the two commonest failures happen before an
-- investigation id is known -- an unreadable fold has only a run id, and a
-- refusal is frequently refused *because* the payload carries no investigation
-- id (core/memory/distil.py `_accept`). So a hunt is keyed by its run and a
-- Case by its case id.
--
-- The vocabularies are spelled as CHECK constraints, as every other table in
-- this directory spells them. `reason` is declared for the writing code in
-- core/memory/recall_contract.py, and a value added there must be added here.

CREATE TABLE IF NOT EXISTS episodic_distil_failures (
    investigation_kind text        NOT NULL CHECK (investigation_kind IN ('hunt', 'case', 'analyst')),
    -- A run id for a hunt, a case id for a Case. Text rather than uuid because
    -- one column carries both, and the poll casts the run id to match. Not
    -- called a subject: a subject is an entity a Verdict names (ADR 0016), and
    -- this is the key the poll held when the write failed.
    failure_key        text        NOT NULL,
    -- The seq of the terminal this failed on. Not a breadcrumb: the hunt poll
    -- compares it against later terminals on the same run, which is what lets a
    -- run that concluded again be re-offered rather than staying behind a
    -- refusal recorded at an earlier one. Absent on a Case, which was closed and
    -- never run; the CHECK below ties its absence to the kind rather than
    -- leaving a reader to guess, exactly as the marker's origin pair does.
    origin_seq         integer,
    reason             text        NOT NULL CHECK (reason IN ('refused', 'unreadable', 'failed')),
    -- Across ticks, not within one: the in-tick retry is bounded by ATTEMPTS and
    -- is not what this counts. A number climbing into the dozens is what makes
    -- "stuck" a query rather than a hunch.
    attempts           integer     NOT NULL CHECK (attempts > 0),
    -- The last one, not every one: a failure that keeps happening keeps saying
    -- the same thing, and a history of identical strings is a table that grows
    -- without telling anyone more than its count already does.
    last_error         text        NOT NULL,
    first_failed_at    timestamptz NOT NULL,
    last_failed_at     timestamptz NOT NULL,
    -- When the poll may offer this subject again. Both polls compare this one
    -- column and nothing else, which is why a refusal is a far-future value
    -- here rather than a second rule beside it. What the schedule is, and why it
    -- paces rather than gives up, is core/memory/distil.py's to state.
    next_attempt_at    timestamptz NOT NULL,
    -- The mapping version this failed at. Matched in the poll's join the way the
    -- marker's is, which is what makes a version bump re-offer everything that
    -- failed under the old mapping, refusals included: a mapping change is the
    -- fix for most of what lands here.
    distil_version     integer     NOT NULL,

    PRIMARY KEY (investigation_kind, failure_key),

    CONSTRAINT episodic_distil_failures_seq_matches_kind CHECK (
        (investigation_kind = 'hunt') = (origin_seq IS NOT NULL)
    ),
    CONSTRAINT episodic_distil_failures_window CHECK (first_failed_at <= last_failed_at)
);

-- No index beyond the primary key. Both polls join on the full key, and the
-- table is bounded by how many subjects are failing at once, which is small by
-- construction -- a successful write deletes its row.

COMMENT ON TABLE episodic_distil_failures IS
    'One row per subject a Distil could not write, with why, how often, and when it may be tried again.';
