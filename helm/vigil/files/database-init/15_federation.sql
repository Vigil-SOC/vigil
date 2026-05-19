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

-- NOTE: the `external_id` column and the `uniq_findings_source_extid`
-- partial unique index on `findings` are declared on the ORM model
-- (`database/models.py` `Finding`) and materialized by SQLAlchemy's
-- create_all() at backend startup — not here. An earlier revision of
-- this file ran `ALTER TABLE findings ADD COLUMN ...` + `CREATE UNIQUE
-- INDEX ... ON findings ...` at init time, but `findings` doesn't exist
-- yet when init SQL runs (the table is created by the ORM after the
-- backend boots). docker-compose's postgres entrypoint runs init files
-- under `set -Eeo pipefail` with `psql -v ON_ERROR_STOP=1`, so the ALTER
-- aborted the entrypoint before the system_config seed below could land
-- — leaving the federation feature silently unconfigured on every fresh
-- docker-compose stack.

-- Seed the global federation toggle (off by default — opt-in feature).
INSERT INTO system_config (key, value, description, config_type)
VALUES (
    'federation.settings',
    '{"enabled": false}'::jsonb,
    'Federated monitoring global on/off (per-source toggles live in federation_sources)',
    'federation'
)
ON CONFLICT (key) DO NOTHING;
