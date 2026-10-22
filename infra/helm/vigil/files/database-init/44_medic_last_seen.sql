-- When the backend last reached Medic (C5 §5.3, D2-18). Exactly one row, written
-- only by the backend's poller (core/platform/medic_last_seen.py) once a minute;
-- none until the first poll, and none while Medic is off. Every time is the
-- backend's clock. It lives here, not in Redis or memory, so it survives a
-- backend restart and Helm replicas read the same answer.
-- status_snapshot holds typed fields from Medic's status op only (K1 §6 G2).
-- scripts/migrate_schema.py runs this same statement.

CREATE TABLE IF NOT EXISTS medic_last_seen (
    id SMALLINT PRIMARY KEY DEFAULT 1 CONSTRAINT medic_last_seen_one_row CHECK (id = 1),
    last_seen_at TIMESTAMP,
    first_failed_at TIMESTAMP,
    failure_kind VARCHAR(8) CONSTRAINT medic_last_seen_failure_kind
        CHECK (failure_kind IN ('refused', 'timeout', '401', '5xx')),
    status_snapshot JSONB CONSTRAINT medic_last_seen_snapshot_typed
        CHECK (jsonb_typeof(status_snapshot) = 'object'
               AND octet_length(status_snapshot::text) <= 4096),
    updated_at TIMESTAMP NOT NULL
);
