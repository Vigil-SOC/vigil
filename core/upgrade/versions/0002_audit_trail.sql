-- The audit trail (#2755 decisions 5, 6, 8 and 9). core/audit/trail.py is its
-- only writer.
--
-- One chain over the whole table. A BEFORE INSERT trigger takes the trail's
-- transaction-scoped advisory lock, allocates seq as the tip plus one (a
-- sequence would gap on rollback, and a gap is a break) and hashes with the
-- ledger's formula, agent_event_hash. Writers supply neither. The hashed
-- document is the payload plus seq, occurred_at and the late flag. Personal
-- details sit in the columns beside it, so erasing them keeps the chain intact.

CREATE TABLE public.audit_trail (
    seq bigint PRIMARY KEY,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    late boolean DEFAULT false NOT NULL,
    payload jsonb NOT NULL,
    prev_hash text NOT NULL,
    entry_hash text NOT NULL,
    -- Beside the hash.
    source_ip text,
    actor_text text,
    operator_text text,
    attempted_name text,
    personal jsonb,
    CONSTRAINT audit_trail_payload_check CHECK (
        jsonb_typeof(payload) = 'object'
        AND NOT payload ?| ARRAY['seq', 'occurred_at', 'late']
    )
);

COMMENT ON TABLE public.audit_trail IS
    'Append-only audit trail. vigil_app may SELECT, INSERT and UPDATE the beside-columns only; audit_trail_delete_prefix is the only delete.';
COMMENT ON COLUMN public.audit_trail.entry_hash IS
    'audit_trail_hash(prev_hash, seq, occurred_at, late, payload), assigned on INSERT and never by the caller.';

-- Where verify starts: the seq and entry_hash of the last entry a prefix
-- delete removed, or (0, '') while nothing has been removed. Outside the chain.
CREATE TABLE public.audit_trail_anchor (
    id smallint DEFAULT 1 PRIMARY KEY CHECK (id = 1),
    seq bigint NOT NULL,
    entry_hash text NOT NULL
);
INSERT INTO public.audit_trail_anchor (id, seq, entry_hash) VALUES (1, 0, '');

-- Rows waiting for the trail, visible to every replica and kept across a
-- restart: a safety action's entry recorded late (window_key NULL), or an open
-- 60-second collapse window (window_key the sha256 of its key).
CREATE TABLE public.audit_trail_held (
    id bigserial PRIMARY KEY,
    window_key text UNIQUE,
    occurred_at timestamp with time zone NOT NULL,
    last_at timestamp with time zone,
    count integer DEFAULT 1 NOT NULL,
    first_seq bigint,
    payload jsonb NOT NULL,
    beside jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE FUNCTION public.audit_trail_hash(
    prev_hash text, seq bigint, occurred_at timestamp with time zone,
    late boolean, payload jsonb
) RETURNS text
LANGUAGE sql STABLE
SET search_path = pg_catalog, public, pg_temp
AS $$
  SELECT public.agent_event_hash(prev_hash, payload || jsonb_build_object(
    'seq', seq,
    'late', late,
    -- UTC text, so the hash does not depend on the session's TimeZone.
    'occurred_at',
    to_char(occurred_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
  ));
$$;

-- Every function here names its tables in full and searches pg_temp last, so
-- a temporary table cannot stand in for the trail or its anchor.
CREATE FUNCTION public.audit_trail_assign() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
BEGIN
  -- 0x766967696C415544, "vigilAUD": the trail's own key.
  PERFORM pg_advisory_xact_lock(8532464671515563332);
  SELECT t.seq, t.entry_hash INTO NEW.seq, NEW.prev_hash
    FROM public.audit_trail t ORDER BY t.seq DESC LIMIT 1;
  IF NOT FOUND THEN
    SELECT a.seq, a.entry_hash INTO NEW.seq, NEW.prev_hash FROM public.audit_trail_anchor a;
  END IF;
  NEW.seq := NEW.seq + 1;
  NEW.entry_hash := public.audit_trail_hash(
    NEW.prev_hash, NEW.seq, NEW.occurred_at, NEW.late, NEW.payload);
  RETURN NEW;
END;
$$;

CREATE TRIGGER audit_trail_assign
  BEFORE INSERT ON public.audit_trail
  FOR EACH ROW
  EXECUTE FUNCTION public.audit_trail_assign();

-- The only delete: a whole oldest prefix, through seq ``through``, with the
-- anchor moved to it in the same transaction. It also takes the prune key
-- ("vigilPRN") that verify holds shared, so a running verify sees no rows vanish.
CREATE FUNCTION public.audit_trail_delete_prefix(through bigint) RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
  last_hash text;
  removed bigint;
BEGIN
  PERFORM pg_advisory_xact_lock(8532464671516545614);
  PERFORM pg_advisory_xact_lock(8532464671515563332);
  SELECT entry_hash INTO last_hash FROM public.audit_trail WHERE seq = through;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'audit trail has no entry %', through;
  END IF;
  DELETE FROM public.audit_trail WHERE seq <= through;
  GET DIAGNOSTICS removed = ROW_COUNT;
  UPDATE public.audit_trail_anchor SET seq = through, entry_hash = last_hash;
  RETURN removed;
END;
$$;

-- Default privileges give vigil_app every write on a new table; take back all
-- but INSERT and the beside-columns' UPDATE (the agent_events pattern).
REVOKE ALL ON TABLE public.audit_trail FROM vigil_app;
GRANT SELECT, INSERT ON TABLE public.audit_trail TO vigil_app;
GRANT UPDATE (source_ip, actor_text, operator_text, attempted_name, personal)
  ON TABLE public.audit_trail TO vigil_app;
REVOKE ALL ON TABLE public.audit_trail_anchor FROM vigil_app;
GRANT SELECT ON TABLE public.audit_trail_anchor TO vigil_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.audit_trail_held TO vigil_app;
GRANT USAGE, SELECT ON SEQUENCE public.audit_trail_held_id_seq TO vigil_app;
REVOKE EXECUTE ON FUNCTION public.audit_trail_delete_prefix(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.audit_trail_delete_prefix(bigint) TO vigil_app;

-- audit.read: the screen and export (Admin, Manager). audit.forward: the feed
-- and the polling API; audit.manage: retention and erasure (Admin only, not
-- settings.write, which Manager holds). Existing keys win, as in
-- 45_medic_permissions.sql.
UPDATE public.roles AS r
SET permissions = g.grants || r.permissions,
    updated_at = NOW()
FROM (VALUES
    ('role-admin',
     '{"audit.read": true, "audit.forward": true, "audit.manage": true}'::jsonb),
    ('role-manager', '{"audit.read": true}'::jsonb)
) AS g (role_id, grants)
WHERE r.role_id = g.role_id
  AND NOT r.permissions ?& ARRAY(SELECT jsonb_object_keys(g.grants));

-- trail_ready() in core/audit/trail.py reads the revision as the app. Reading
-- only: the owner still moves it.
GRANT SELECT ON TABLE public.alembic_version TO vigil_app;
