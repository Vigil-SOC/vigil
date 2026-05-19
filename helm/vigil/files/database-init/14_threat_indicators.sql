-- 14_threat_indicators.sql
-- Global threat-indicator pool sourced from external feeds (Cloudforce One STIX/TAXII,
-- and any future feed-driven sources). Distinct from `case_iocs`, which is case-scoped.
-- Indicators are pulled by daemon/threat_feed_poller.py and used during finding
-- enrichment in daemon/processor.py.

CREATE TABLE IF NOT EXISTS threat_indicators (
    id              BIGSERIAL PRIMARY KEY,
    indicator_type  VARCHAR(32)  NOT NULL,         -- ip, domain, url, hash_md5, hash_sha1, hash_sha256, email
    indicator_value VARCHAR(2048) NOT NULL,
    source          VARCHAR(64)  NOT NULL,         -- 'cloudforce_one', etc.
    collection_id   VARCHAR(128),                  -- TAXII collection that emitted this indicator
    confidence      NUMERIC(5,2),                  -- 0..100, normalized from STIX confidence
    threat_level    VARCHAR(16),                   -- critical, high, medium, low, info
    labels          TEXT[]        DEFAULT ARRAY[]::TEXT[],
    valid_from      TIMESTAMP,
    valid_until     TIMESTAMP,
    raw_stix        JSONB,
    first_seen      TIMESTAMP    NOT NULL DEFAULT NOW(),
    last_seen       TIMESTAMP    NOT NULL DEFAULT NOW(),

    CONSTRAINT threat_indicators_unique UNIQUE (source, indicator_type, indicator_value)
);

CREATE INDEX IF NOT EXISTS idx_threat_indicators_type_value
    ON threat_indicators (indicator_type, indicator_value);

CREATE INDEX IF NOT EXISTS idx_threat_indicators_source
    ON threat_indicators (source);

CREATE INDEX IF NOT EXISTS idx_threat_indicators_last_seen
    ON threat_indicators (last_seen DESC);

CREATE INDEX IF NOT EXISTS idx_threat_indicators_valid_until
    ON threat_indicators (valid_until);
