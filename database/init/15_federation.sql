-- 15_federation.sql
-- Federated monitoring: per-source polling state + global toggle.
--
-- Each row represents one data source (Splunk, CrowdStrike, etc.) that the
-- daemon's federation poller pulls from on a configurable cadence. Rows are
-- auto-seeded on daemon boot from configured integrations (default disabled);
-- the global on/off lives in `system_config` under key `federation.settings`.

CREATE TABLE IF NOT EXISTS federation_sources (
    source_id           VARCHAR(64)  PRIMARY KEY,
    enabled             BOOLEAN      NOT NULL DEFAULT FALSE,
    interval_seconds    INTEGER      NOT NULL DEFAULT 300,
    max_items           INTEGER      NOT NULL DEFAULT 100,
    min_severity        VARCHAR(16),                       -- nullable: "low"|"medium"|"high"|"critical"
    cursor              JSONB        NOT NULL DEFAULT '{}'::jsonb,
    last_poll_at        TIMESTAMPTZ,
    last_success_at     TIMESTAMPTZ,
    last_error          TEXT,
    consecutive_errors  INTEGER      NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_federation_sources_enabled
    ON federation_sources (enabled);

-- Add external_id to findings to support a strong (data_source, external_id)
-- dedup guarantee. Existing rows leave external_id NULL (the partial unique
-- index below excludes NULLs), so legacy findings are not affected.
ALTER TABLE findings
    ADD COLUMN IF NOT EXISTS external_id VARCHAR(255);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_findings_source_extid
    ON findings (data_source, external_id)
    WHERE data_source IS NOT NULL AND external_id IS NOT NULL;

-- Seed the global federation toggle (off by default — opt-in feature).
INSERT INTO system_config (key, value, description, config_type)
VALUES (
    'federation.settings',
    '{"enabled": false}'::jsonb,
    'Federated monitoring global on/off (per-source toggles live in federation_sources)',
    'federation'
)
ON CONFLICT (key) DO NOTHING;
