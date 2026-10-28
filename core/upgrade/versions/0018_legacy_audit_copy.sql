-- The old audit stores' copy onto the trail (#3309, #2755 decision 14).
--
-- investigation_logs keeps its rows when an investigation is deleted: the
-- foreign key is re-added without ON DELETE CASCADE, so a delete is refused
-- while an investigation still has log rows.

ALTER TABLE public.investigation_logs
    DROP CONSTRAINT IF EXISTS investigation_logs_investigation_id_fkey;
ALTER TABLE public.investigation_logs
    ADD CONSTRAINT investigation_logs_investigation_id_fkey
    FOREIGN KEY (investigation_id) REFERENCES public.investigations(investigation_id);

-- One row: what the copy found, written by the revision, and the read-only
-- stamp, which only `python -m core.upgrade` sets, after the revisions commit.
-- The six-month clock before the old tables are dropped starts at the stamp.
CREATE TABLE IF NOT EXISTS public.audit_legacy_copy (
    id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    -- {"<table>": {"rows": n, "on_trail": n, "copied": n}, ...}
    sources jsonb NOT NULL,
    copied bigint NOT NULL,
    passed boolean NOT NULL,
    reconciled_at timestamp with time zone DEFAULT now() NOT NULL,
    read_only_since timestamp with time zone
);

COMMENT ON TABLE public.audit_legacy_copy IS
    'Reconciliation of the copy of case_audit_logs, config_audit_log and investigation_logs onto the audit trail, and the read-only stamp. Outside the chain. One row.';

REVOKE ALL ON TABLE public.audit_legacy_copy FROM vigil_app;
GRANT SELECT ON TABLE public.audit_legacy_copy TO vigil_app;
