-- Integration Health (#4869, epic #4866): one record per source. Scheduled
-- collection, Test and detection-source syncs write it (core/integrations/
-- health.py); federation_sources' health columns and integration_configs'
-- last_test_* stop being written and are dropped in a later release.
-- Naive UTC like the rest of the schema (core/time.py).
CREATE TABLE IF NOT EXISTS public.source_health (
    -- An integration id, or a prefixed id for Vigil's own inputs
    -- (kafka, webhook:<name>, detection_source:<id>).
    source_id VARCHAR(128) PRIMARY KEY,
    last_attempt_at TIMESTAMP WITHOUT TIME ZONE,
    last_attempt_ok BOOLEAN,
    last_error TEXT,
    last_error_kind TEXT
        CONSTRAINT source_health_last_error_kind_check
        CHECK (last_error_kind IN ('auth','network','config','quota','upstream','unknown')),
    last_success_at TIMESTAMP WITHOUT TIME ZONE,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    dropped_total INTEGER NOT NULL DEFAULT 0,
    dropped_window_start TIMESTAMP WITHOUT TIME ZONE,
    dropped_window INTEGER NOT NULL DEFAULT 0,
    last_dropped_at TIMESTAMP WITHOUT TIME ZONE,
    last_data_at TIMESTAMP WITHOUT TIME ZONE,
    last_check_at TIMESTAMP WITHOUT TIME ZONE,
    last_check_ok BOOLEAN,
    last_check_error TEXT,
    reset_at TIMESTAMP WITHOUT TIME ZONE,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT now()
);

-- Carry both old homes across. A federation row is keyed by its adapter name,
-- which for four sources is not the integration id. Where one id has both, the
-- later of the last poll and the last Test is the last attempt. last_test_* is
-- the last real check only for the integrations with a credential check
-- (core/integrations/credential_check.py); for the rest it was only a probe.
WITH fed AS (
    SELECT CASE source_id
               WHEN 'elastic' THEN 'elastic-siem'
               WHEN 'azure_sentinel' THEN 'azure-sentinel'
               WHEN 'aws_security_hub' THEN 'aws-security-hub'
               WHEN 'microsoft_defender' THEN 'microsoft-defender'
               ELSE source_id
           END AS id,
           -- timestamptz there; naive UTC here, whatever the session zone.
           last_poll_at AT TIME ZONE 'UTC' AS last_poll_at,
           last_success_at AT TIME ZONE 'UTC' AS last_success_at,
           last_error, last_error_kind, consecutive_errors, dropped_total
    FROM public.federation_sources
), tested AS (
    SELECT integration_id AS id, last_test_at, last_test_success, last_error,
           integration_id IN (
               'splunk', 'elastic-siem', 'opensearch', 'wazuh', 'opencti',
               'azure-sentinel', 'aws-security-hub', 'amazon-security-lake',
               'microsoft-defender', 'crowdstrike'
           ) AS real_check
    FROM public.integration_configs
    WHERE last_test_at IS NOT NULL
), merged AS (
    SELECT coalesce(f.id, t.id) AS sid, f.last_poll_at, f.last_success_at,
           f.last_error, f.last_error_kind, f.consecutive_errors, f.dropped_total,
           t.last_test_at, t.last_test_success,
           t.last_error AS test_error, coalesce(t.real_check, false) AS real_check,
           t.last_test_at IS NOT NULL
               AND (f.last_poll_at IS NULL OR t.last_test_at > f.last_poll_at)
               AS test_latest
    FROM fed f FULL JOIN tested t ON t.id = f.id
)
INSERT INTO public.source_health (
    source_id, last_attempt_at, last_attempt_ok, last_error, last_error_kind,
    last_success_at, consecutive_failures, dropped_total,
    last_check_at, last_check_ok, last_check_error
)
SELECT sid,
       CASE WHEN test_latest THEN last_test_at ELSE last_poll_at END,
       CASE WHEN test_latest THEN last_test_success
            WHEN last_poll_at IS NOT NULL THEN coalesce(consecutive_errors, 0) = 0
       END,
       CASE WHEN test_latest THEN test_error ELSE last_error END,
       CASE WHEN test_latest THEN NULL ELSE last_error_kind END,
       last_success_at,
       coalesce(consecutive_errors, 0),
       coalesce(dropped_total, 0),
       CASE WHEN real_check THEN last_test_at END,
       CASE WHEN real_check THEN last_test_success END,
       CASE WHEN real_check THEN test_error END
FROM merged
ON CONFLICT (source_id) DO NOTHING;

GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.source_health TO vigil_app;
