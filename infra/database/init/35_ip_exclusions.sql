-- IP addresses an analyst has excluded from the findings queue and from what a
-- hunt or investigation starts from.
--
-- An exclusion is analyst metadata about an address, never an edit to the
-- findings that carry it. Ingest does not read this table: LogLM and every
-- other source keep writing findings for an excluded address, with the severity
-- and status they would have had, so removing the exclusion restores them as
-- they were. What reads it is the console's findings list, which hides a finding
-- when any address it names is excluded, and the agent layer, which does not
-- seed a run from such a finding.
--
-- One address per row, and no ranges: an analyst excludes the address in front
-- of them, and a CIDR written in a hurry hides more than they looked at.
--
-- Removal is recorded, not deleted: "this address was hidden from 09:10 to
-- 14:02, by whom and why" is what explains a gap in the queue afterwards. At
-- most one active row per address; excluding it again after removal is a new
-- row with its own reason.

CREATE TABLE IF NOT EXISTS ip_exclusions (
    exclusion_id VARCHAR(50) PRIMARY KEY,

    -- Canonical text form (Python's ipaddress: lower-case, compressed IPv6), so
    -- it compares equal to the address as ingest writes it into entity_context.
    ip VARCHAR(45) NOT NULL,

    -- Why the analyst is hiding it. Required: an exclusion nobody can explain
    -- is one nobody dares remove.
    reason TEXT NOT NULL,

    -- Where it was made: from the exclusion list (ad_hoc), from a finding, a
    -- case, or a hunt/investigation run, and which one.
    origin VARCHAR(20) NOT NULL DEFAULT 'ad_hoc',
    origin_ref VARCHAR(100),

    created_by VARCHAR(100) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),

    removed_at TIMESTAMP,
    removed_by VARCHAR(100),
    removal_reason TEXT,

    CONSTRAINT ck_ip_exclusions_origin
        CHECK (origin IN ('ad_hoc', 'finding', 'case', 'run')),
    CONSTRAINT ck_ip_exclusions_removal
        CHECK ((removed_at IS NULL) = (removed_by IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_ip_exclusions_active_ip
    ON ip_exclusions (ip) WHERE removed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_ip_exclusions_created_at
    ON ip_exclusions (created_at DESC);

COMMENT ON TABLE ip_exclusions IS
    'Analyst IP exclusions: hidden from the findings queue and not used to seed hunts/investigations. Ingest ignores this table.';
