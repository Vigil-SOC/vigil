-- Runtime login for app processes. Schema owner stays the cluster POSTGRES_USER.
-- Password is set by the apply wrapper from POSTGRES_PASSWORD; none here.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vigil_app') THEN
    CREATE ROLE vigil_app NOSUPERUSER LOGIN;
  END IF;
  -- Only a superuser may touch SUPERUSER, so a database owner with CREATEROLE
  -- can apply this file unless the role actually needs that fixed.
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vigil_app' AND rolsuper) THEN
    ALTER ROLE vigil_app NOSUPERUSER;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vigil_app' AND NOT rolcanlogin) THEN
    ALTER ROLE vigil_app LOGIN;
  END IF;
END
$$;

-- CREATE stays: adoption compares this file's result with the frozen baseline.
-- Revision 0023_runtime_role_no_create revokes it on the way to head.
GRANT USAGE, CREATE ON SCHEMA public TO vigil_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO vigil_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO vigil_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO vigil_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO vigil_app;

GRANT SELECT, INSERT ON TABLE agent_events TO vigil_app;
REVOKE UPDATE, DELETE, TRUNCATE ON TABLE agent_events FROM vigil_app;

-- Day 1 Report rows are insert-only (0108_report_records). The tables are
-- created by that revision, which is after this file on a fresh install and
-- on adoption, so the revoke runs only once they exist. Re-applying this
-- file grants every table above, and this puts the two back.
DO $$
BEGIN
  IF to_regclass('public.report_runs') IS NOT NULL THEN
    GRANT SELECT, INSERT ON TABLE public.report_runs TO vigil_app;
    REVOKE UPDATE, DELETE, TRUNCATE ON TABLE public.report_runs FROM vigil_app;
  END IF;
  IF to_regclass('public.report_records') IS NOT NULL THEN
    GRANT SELECT, INSERT ON TABLE public.report_records TO vigil_app;
    REVOKE UPDATE, DELETE, TRUNCATE ON TABLE public.report_records FROM vigil_app;
  END IF;
END
$$;
