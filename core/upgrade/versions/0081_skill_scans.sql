-- Scan verdicts for operator skills, keyed by the digest of the skill folder.
-- A changed file is a new digest and so a new row: a verdict never carries over
-- to different content. core/skills/skill_hold.py reads and writes it.
-- Times are naive UTC like the rest of the schema (core/time.py); the default
-- converts, so a row's time does not depend on the session's timezone.
-- approved_by / approved_at are the person who released a skill the scan did
-- not call safe; a skill is held until its digest is SAFE or approved.

CREATE TABLE IF NOT EXISTS public.skill_scans (
    digest TEXT PRIMARY KEY,
    skill_name TEXT NOT NULL,
    status TEXT NOT NULL,
    recommendation TEXT,
    score INTEGER,
    max_severity TEXT,
    findings JSONB NOT NULL DEFAULT '[]'::jsonb,
    scanner_version TEXT,
    error TEXT,
    scanned_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    approved_by TEXT,
    approved_at TIMESTAMP WITHOUT TIME ZONE,
    CONSTRAINT skill_scans_status CHECK (status IN ('scanned', 'failed')),
    CONSTRAINT skill_scans_recommendation CHECK (
        recommendation IS NULL
        OR recommendation IN ('SAFE', 'CAUTION', 'DO_NOT_INSTALL')
    )
);

CREATE INDEX IF NOT EXISTS idx_skill_scans_name ON public.skill_scans (skill_name);

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.skill_scans TO vigil_app;
