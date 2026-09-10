-- Runtime login for app processes. Schema owner stays the cluster POSTGRES_USER.
-- Password is set by the apply wrapper from POSTGRES_PASSWORD; none here.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vigil_app') THEN
    CREATE ROLE vigil_app NOSUPERUSER LOGIN;
  END IF;
END
$$;

ALTER ROLE vigil_app NOSUPERUSER LOGIN;

GRANT USAGE, CREATE ON SCHEMA public TO vigil_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO vigil_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO vigil_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO vigil_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO vigil_app;

GRANT SELECT, INSERT ON TABLE agent_events TO vigil_app;
REVOKE UPDATE, DELETE, TRUNCATE ON TABLE agent_events FROM vigil_app;
