-- The only delete of a re-decision ledger. vigil_app may call it and still
-- may not DELETE agent_events; vigil_agent is not granted either. Any id
-- that is missing or not marked raises before either delete, so the call
-- removes nothing. agent_events.run_id is uuid; workflow_runs.run_id is
-- text and the starter stores the uuid's text. There is no FK between them.
-- workflow_run_phases cascades; approval_actions.workflow_run_id sets null.

CREATE FUNCTION public.erase_redecision_ledgers(run_ids text[]) RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  wanted integer;
  marked integer;
  removed integer;
  given text;
BEGIN
  IF run_ids IS NULL OR cardinality(run_ids) = 0 THEN
    RAISE EXCEPTION 're-decision erase needs run ids'
      USING ERRCODE = '22023';
  END IF;

  SELECT count(*) INTO wanted FROM unnest(run_ids) AS g(run_id);

  PERFORM 1
  FROM public.workflow_runs
  WHERE run_id IN (SELECT g.run_id FROM unnest(run_ids) AS g(run_id))
  ORDER BY run_id
  FOR UPDATE;

  SELECT count(*) INTO marked
  FROM unnest(run_ids) AS g(run_id)
  JOIN public.workflow_runs AS w ON w.run_id = g.run_id
  WHERE w.trigger_context -> 're_decision' = 'true'::jsonb;

  IF marked IS DISTINCT FROM wanted THEN
    RAISE EXCEPTION 're-decision erase refused: a run is not marked'
      USING ERRCODE = '22023';
  END IF;

  -- The ledger writer holds this lock for the whole append (services/agent/ledger/repository.ts).
  -- Take it first, in run_id order, so an append in flight commits and is then deleted,
  -- and this call cannot miss those rows.
  FOR given IN
    SELECT DISTINCT g.run_id
    FROM unnest(run_ids) AS g(run_id)
    ORDER BY g.run_id
  LOOP
    PERFORM pg_advisory_xact_lock(hashtext(given)::bigint);
  END LOOP;

  DELETE FROM public.agent_events
  WHERE run_id IN (
    SELECT g.run_id::uuid FROM unnest(run_ids) AS g(run_id)
  );

  DELETE FROM public.workflow_runs
  WHERE run_id IN (SELECT g.run_id FROM unnest(run_ids) AS g(run_id));
  GET DIAGNOSTICS removed = ROW_COUNT;
  RETURN removed;
END;
$$;

REVOKE ALL ON FUNCTION public.erase_redecision_ledgers(text[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.erase_redecision_ledgers(text[]) TO vigil_app;

-- Neither login can delete the table on its own. The revoke is a no-op
-- where the grant was never given.
REVOKE DELETE ON TABLE public.agent_events FROM vigil_app;
REVOKE DELETE ON TABLE public.agent_events FROM vigil_agent;

UPDATE public.roles
SET permissions = permissions || '{"ledgers.erase": true}'::jsonb,
    updated_at = NOW()
WHERE role_id = 'role-admin';
