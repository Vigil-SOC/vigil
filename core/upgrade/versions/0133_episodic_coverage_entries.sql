-- One row per key per check (#5293): which investigation looked at an Entity Key
-- or technique ID, how it came out, and who concluded it. Written by the hunt and
-- Case Distils beside their Verdicts and Gaps, uncapped, and withdrawn with them.
-- A reopen row (method 'reopen') names no investigation and carries no outcome.
CREATE TABLE IF NOT EXISTS public.episodic_coverage_entries (
    id BIGSERIAL PRIMARY KEY,
    key TEXT NOT NULL,
    investigation_kind TEXT
        CONSTRAINT episodic_coverage_entries_investigation_kind_check
        CHECK (investigation_kind IN ('hunt', 'case', 'analyst')),
    investigation_id TEXT,
    run_id UUID,
    method TEXT NOT NULL
        CONSTRAINT episodic_coverage_entries_method_check
        CHECK (method IN ('hunt', 'case', 'reopen')),
    outcome TEXT
        CONSTRAINT episodic_coverage_entries_outcome_check
        CHECK (outcome IN ('proven', 'disproven', 'inconclusive', 'handed_off')),
    trust TEXT NOT NULL
        CONSTRAINT episodic_coverage_entries_trust_check
        CHECK (trust IN ('agent', 'analyst')),
    concluded_by TEXT,
    concluded_at TIMESTAMP WITH TIME ZONE NOT NULL,
    reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_episodic_coverage_entries_key
    ON public.episodic_coverage_entries (key, concluded_at DESC);

-- What clear_investigation deletes by.
CREATE INDEX IF NOT EXISTS idx_episodic_coverage_entries_investigation
    ON public.episodic_coverage_entries (investigation_kind, investigation_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.episodic_coverage_entries TO vigil_app;
GRANT USAGE, SELECT ON SEQUENCE public.episodic_coverage_entries_id_seq TO vigil_app;
