-- Episodic memory: what investigations saw and what they concluded, joined on
-- entity keys. Written by the Distil (core/memory/distil.py), read by recall.
--
-- Everything is keyed by investigation_kind + investigation_id and never by
-- run_id: a run-keyed schema cannot represent the Case-authored Verdicts that
-- follow, which have a Case behind them and no run at all.
--
-- No column is nullable, with one stated exception. An empty list means
-- known-to-be-none, and there is no unknown state to represent -- a row nobody
-- can read as "we did not record this" is a row nobody has to guess about. The
-- exception is the marker's origin pair, which a Case has no value for because
-- a Case is closed and never run; a CHECK ties its absence to the kind, so it
-- still names one thing rather than leaving a reader to guess.

-- The vocabularies are spelled as CHECK constraints rather than as enum types:
-- altering an enum takes a lock this schema does not need, and every other table
-- in this directory states its domains the same way. This file is where they are
-- enforced; core/memory/recall_contract.py is where they are declared for the
-- code that writes them, and a value added there must be added here too.

-- What an investigation observed. One row per entity, investigation and source,
-- so growth tracks investigations and not telemetry volume: a hunt that saw one
-- address ten thousand times in one system writes one row with a hit count.
CREATE TABLE IF NOT EXISTS episodic_sightings (
    id                     bigserial   PRIMARY KEY,
    -- `type:value`, normalised by Python. Only Python writes an Entity Key; the
    -- harness's extractor output arrives as candidate type and value.
    entity_key             text        NOT NULL,
    investigation_kind     text        NOT NULL CHECK (investigation_kind IN ('hunt', 'case', 'analyst')),
    investigation_id       text        NOT NULL,
    -- Memory's own column, not a foreign key into either producer: a hunt fills
    -- it from the Ledger's source_system, a Case from its Findings' data_source.
    source_system          text        NOT NULL,
    hit_count              integer     NOT NULL CHECK (hit_count > 0),
    -- Aggregated over the group: one record an adversary could have authored is
    -- enough to say so of the group.
    attacker_influenceable boolean     NOT NULL,
    -- Both ends inclusive, and observed rather than asserted -- a Sighting that
    -- did not observe its window is not a Sighting.
    first_seen             timestamptz NOT NULL,
    last_seen              timestamptz NOT NULL,
    -- When the investigation concluded, not when the row was written. The Distil
    -- polls, so an investigation that ended Monday can be written Wednesday
    -- carrying Monday's date.
    concluded_at           timestamptz NOT NULL,

    CONSTRAINT episodic_sightings_unique
        UNIQUE (entity_key, investigation_kind, investigation_id, source_system),
    CONSTRAINT episodic_sightings_window CHECK (first_seen <= last_seen)
);

-- The recall join, and the order it reads in. Ties break on the primary key
-- because a LIMIT over a partial order lets Postgres return a different set on
-- identical data, which surfaces as a replay diff rather than as an error.
CREATE INDEX IF NOT EXISTS idx_episodic_sightings_recall
    ON episodic_sightings (entity_key, concluded_at DESC, id ASC);

-- The Distil's own delete, which is scoped to one investigation.
CREATE INDEX IF NOT EXISTS idx_episodic_sightings_investigation
    ON episodic_sightings (investigation_kind, investigation_id);

COMMENT ON TABLE episodic_sightings IS
    'What an investigation observed: one row per entity, investigation and source, never per evidence record.';

-- What an investigation concluded, one row per Hypothesis.
CREATE TABLE IF NOT EXISTS episodic_verdicts (
    id                          bigserial   PRIMARY KEY,
    investigation_kind          text        NOT NULL CHECK (investigation_kind IN ('hunt', 'case', 'analyst')),
    investigation_id            text        NOT NULL,
    -- Stable, because the prose gets re-worded. A Case's is its case id.
    hypothesis_id               text        NOT NULL,
    statement                   text        NOT NULL,
    outcome                     text        NOT NULL CHECK (
        outcome IN ('proven', 'disproven', 'inconclusive', 'handed_off', 'false_positive')
    ),
    -- Reaches no ranking function: confident-sounding model text must not move a
    -- priority (ADR 0015). Carried for a human reading the run back.
    rationale                   text        NOT NULL,
    -- The entities the Hypothesis named, typically one to three -- not the 38 to
    -- 76 its evidence touched, which stay reachable through Sightings (ADR 0016).
    -- Empty is legitimate: "is there any lateral movement at all" names none, and
    -- such a Verdict is written and reachable only by narrative recall.
    subject_entities            text[]      NOT NULL DEFAULT ARRAY[]::text[],
    attacker_influenceable_only boolean     NOT NULL,
    -- Who concluded, as distinct from what the source is.
    trust                       text        NOT NULL CHECK (trust IN ('analyst', 'agent')),
    first_seen                  timestamptz NOT NULL,
    last_seen                   timestamptz NOT NULL,
    -- A retrospective sweep over old archives asserts its window rather than
    -- observing it, and ranking discounts the weaker one.
    window_source               text        NOT NULL CHECK (window_source IN ('observed', 'asserted')),
    concluded_at                timestamptz NOT NULL,

    CONSTRAINT episodic_verdicts_unique
        UNIQUE (investigation_kind, investigation_id, hypothesis_id),
    CONSTRAINT episodic_verdicts_window CHECK (first_seen <= last_seen)
);

-- Recall joins Verdicts on the subject array rather than on a key column.
CREATE INDEX IF NOT EXISTS idx_episodic_verdicts_subjects
    ON episodic_verdicts USING GIN (subject_entities);

CREATE INDEX IF NOT EXISTS idx_episodic_verdicts_recall
    ON episodic_verdicts (concluded_at DESC, id ASC);

CREATE INDEX IF NOT EXISTS idx_episodic_verdicts_investigation
    ON episodic_verdicts (investigation_kind, investigation_id);

COMMENT ON TABLE episodic_verdicts IS
    'One conclusion per Hypothesis, naming its subject entities and not its evidence''s.';

-- One row per Verdict and source, carrying direction. Replaces a flat
-- corroborated list, which cannot say that a source argued against the claim.
CREATE TABLE IF NOT EXISTS episodic_verdict_sources (
    id            bigserial PRIMARY KEY,
    verdict_id    bigint    NOT NULL REFERENCES episodic_verdicts (id) ON DELETE CASCADE,
    source_system text      NOT NULL,
    stance        text      NOT NULL CHECK (stance IN ('supports', 'weakens', 'neither')),
    -- Stamped at write time and never joined at read time: an integration
    -- removed or recategorised later must not retroactively change how a past
    -- Verdict was corroborated. The harness's own records never reach here --
    -- the fold drops them before a stance is taken -- so `not_evidence` is a
    -- fail-closed guard against a source the tier map regrades later, and is
    -- representable so that such a row is visible rather than silently absent.
    source_tier   text      NOT NULL CHECK (source_tier IN ('telemetry', 'feed', 'not_evidence')),

    CONSTRAINT episodic_verdict_sources_unique UNIQUE (verdict_id, source_system)
);

COMMENT ON TABLE episodic_verdict_sources IS
    'Per-source Stance and stamped Source Tier for one Verdict; the tier is a fact of write time.';

-- Questions an investigation never gathered evidence for. No activity window,
-- which is the reason these are not Verdict rows with an empty outcome.
CREATE TABLE IF NOT EXISTS episodic_gaps (
    id                 bigserial   PRIMARY KEY,
    investigation_kind text        NOT NULL CHECK (investigation_kind IN ('hunt', 'case', 'analyst')),
    investigation_id   text        NOT NULL,
    hypothesis_id      text        NOT NULL,
    statement          text        NOT NULL,
    disposition        text        NOT NULL CHECK (
        disposition IN ('deprioritised', 'no_evidence_gathered', 'budget_exhausted')
    ),
    reason             text        NOT NULL,
    subject_entities   text[]      NOT NULL DEFAULT ARRAY[]::text[],
    concluded_at       timestamptz NOT NULL,

    CONSTRAINT episodic_gaps_unique
        UNIQUE (investigation_kind, investigation_id, hypothesis_id)
);

CREATE INDEX IF NOT EXISTS idx_episodic_gaps_subjects
    ON episodic_gaps USING GIN (subject_entities);

CREATE INDEX IF NOT EXISTS idx_episodic_gaps_recall
    ON episodic_gaps (concluded_at DESC, id ASC);

CREATE INDEX IF NOT EXISTS idx_episodic_gaps_investigation
    ON episodic_gaps (investigation_kind, investigation_id);

COMMENT ON TABLE episodic_gaps IS
    'A question an investigation left unanswered, with why nothing was gathered for it.';

-- The only record that an investigation was processed. Deliberately not derived
-- from the presence of rows: an investigation that concluded nothing writes no
-- Sightings, no Verdicts and no Gaps, and is still done.
CREATE TABLE IF NOT EXISTS episodic_distil_markers (
    investigation_kind text        NOT NULL CHECK (investigation_kind IN ('hunt', 'case', 'analyst')),
    investigation_id   text        NOT NULL,
    -- The terminal these rows were derived from, so a marker can be traced back
    -- to a ledger. Null on a Case, which was closed rather than run.
    origin_run_id      uuid,
    -- The seq of that terminal. A hunt that resumed past its own terminal
    -- appends a second one to the same run, and comparing seq is what makes the
    -- later conclusions re-derive instead of being skipped as a run already seen.
    origin_seq         integer,
    -- Every run this investigation has been distilled from, origin_run_id
    -- included. One investigation can span more than one run -- a resumed hunt
    -- is the case the rest of this schema is keyed for -- and the poll has only
    -- run ids to work with, because agent_events carries no investigation id.
    -- Without this, each run of one investigation misses the other's marker and
    -- both re-distil on every tick forever.
    origin_run_ids     uuid[]      NOT NULL DEFAULT ARRAY[]::uuid[],
    -- Bumped when the mapping changes. Re-deriving is delete-then-insert, so a
    -- bump that now yields fewer rows leaves none of the old ones behind.
    distil_version     integer     NOT NULL,
    -- Set, never incremented: a count that drifts from its rows is worse than no
    -- count, because it reads as authoritative.
    sightings_written  integer     NOT NULL CHECK (sightings_written >= 0),
    verdicts_written   integer     NOT NULL CHECK (verdicts_written >= 0),
    gaps_written       integer     NOT NULL CHECK (gaps_written >= 0),
    concluded_at       timestamptz NOT NULL,
    distilled_at       timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (investigation_kind, investigation_id),

    -- The one place this schema admits a NULL, and it is not an unknown state:
    -- which of the two shapes a marker has follows entirely from its kind, and
    -- this says so. A hunt marker names the terminal it was derived from; a
    -- Case marker cannot, because a Case is closed and never run. Stated as a
    -- CHECK rather than left to the writer so that neither shape can be
    -- half-written -- a hunt marker with no origin is a marker nothing can be
    -- traced back to, and a Case marker carrying one is a lie about a run.
    CONSTRAINT episodic_distil_markers_origin_matches_kind CHECK (
        (investigation_kind = 'hunt') = (origin_run_id IS NOT NULL)
        AND (origin_run_id IS NULL) = (origin_seq IS NULL)
    )
);

-- Existing databases predate the Case writer, which needs the origin columns to
-- be absent on a kind that has no run.
ALTER TABLE episodic_distil_markers ALTER COLUMN origin_run_id DROP NOT NULL;
ALTER TABLE episodic_distil_markers ALTER COLUMN origin_seq    DROP NOT NULL;
ALTER TABLE episodic_distil_markers
    DROP CONSTRAINT IF EXISTS episodic_distil_markers_origin_matches_kind;
ALTER TABLE episodic_distil_markers
    ADD CONSTRAINT episodic_distil_markers_origin_matches_kind CHECK (
        (investigation_kind = 'hunt') = (origin_run_id IS NOT NULL)
        AND (origin_run_id IS NULL) = (origin_seq IS NULL)
    );

-- The Distil's poll: which terminals no marker at the current version covers.
CREATE INDEX IF NOT EXISTS idx_episodic_markers_origin
    ON episodic_distil_markers USING GIN (origin_run_ids);

COMMENT ON TABLE episodic_distil_markers IS
    'One row per distilled investigation, written in the same transaction as its rows.';
