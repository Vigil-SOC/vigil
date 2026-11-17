-- The agent layer (agent-serve, agent-worker) logs in as vigil_agent, not
-- vigil_app. It reads and writes the run ledger, run leases and operator
-- directives and nothing else, so a compromised agent container holds no
-- write on users, sign-ins, roles or credentials. The apply wrapper sets the
-- password from POSTGRES_AGENT_PASSWORD; none here.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vigil_agent') THEN
    CREATE ROLE vigil_agent NOSUPERUSER LOGIN;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vigil_agent' AND rolsuper) THEN
    ALTER ROLE vigil_agent NOSUPERUSER;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vigil_agent' AND NOT rolcanlogin) THEN
    ALTER ROLE vigil_agent LOGIN;
  END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO vigil_agent;
-- Append-only, as for vigil_app.
GRANT SELECT, INSERT ON TABLE public.agent_events TO vigil_agent;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.agent_run_leases TO vigil_agent;
GRANT SELECT, INSERT ON TABLE public.agent_directives TO vigil_agent;
GRANT USAGE ON SEQUENCE public.agent_directives_id_seq TO vigil_agent;
