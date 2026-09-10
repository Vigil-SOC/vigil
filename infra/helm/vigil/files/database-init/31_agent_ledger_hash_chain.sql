-- Hash chain on agent_events: filled on INSERT so every writer shares one formula.
-- prev_hash is empty at the first seq; event_hash is sha256(prev_hash || payload text).

ALTER TABLE agent_events ADD COLUMN IF NOT EXISTS prev_hash text;
ALTER TABLE agent_events ADD COLUMN IF NOT EXISTS event_hash text;

COMMENT ON COLUMN agent_events.prev_hash IS
    'event_hash of the previous seq in this run, or empty at the first row.';
COMMENT ON COLUMN agent_events.event_hash IS
    'sha256(prev_hash || CAST(payload AS text)), assigned on INSERT and never by the caller.';

CREATE OR REPLACE FUNCTION agent_event_hash(prev_hash text, payload jsonb)
RETURNS text
LANGUAGE sql
IMMUTABLE
SET search_path = public
AS $$
  SELECT encode(
    sha256(convert_to(coalesce(prev_hash, '') || CAST(payload AS text), 'UTF8')),
    'hex'
  );
$$;

CREATE OR REPLACE FUNCTION agent_events_assign_hashes()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = public
AS $$
BEGIN
  SELECT event_hash INTO NEW.prev_hash
    FROM agent_events
    WHERE run_id = NEW.run_id AND seq < NEW.seq
    ORDER BY seq DESC
    LIMIT 1;
  NEW.prev_hash := coalesce(NEW.prev_hash, '');
  NEW.event_hash := agent_event_hash(NEW.prev_hash, NEW.payload);
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS agent_events_assign_hashes ON agent_events;
CREATE TRIGGER agent_events_assign_hashes
  BEFORE INSERT ON agent_events
  FOR EACH ROW
  EXECUTE FUNCTION agent_events_assign_hashes();

DO $$
DECLARE
  rec record;
  prev text;
BEGIN
  FOR rec IN
    SELECT run_id, seq, payload
      FROM agent_events
     WHERE event_hash IS NULL
     ORDER BY run_id, seq
  LOOP
    SELECT event_hash INTO prev
      FROM agent_events
     WHERE run_id = rec.run_id AND seq < rec.seq
     ORDER BY seq DESC
     LIMIT 1;
    prev := coalesce(prev, '');
    UPDATE agent_events
       SET prev_hash = prev,
           event_hash = agent_event_hash(prev, rec.payload)
     WHERE run_id = rec.run_id AND seq = rec.seq;
  END LOOP;
END;
$$;

ALTER TABLE agent_events ALTER COLUMN prev_hash SET NOT NULL;
ALTER TABLE agent_events ALTER COLUMN event_hash SET NOT NULL;
