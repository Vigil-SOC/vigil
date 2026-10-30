-- Each string under a FINDING_IP_KEYS key, and each string in a list there:
-- exactly what ``entity_context[key] ?| ips`` matched. The key list must match
-- FINDING_IP_KEYS in core/storage/ip_address.py.
CREATE OR REPLACE FUNCTION public.finding_ips(entity_context jsonb) RETURNS text[]
LANGUAGE sql IMMUTABLE PARALLEL SAFE
SET search_path = pg_catalog, public, pg_temp
AS $$
  SELECT coalesce(array_agg(DISTINCT ip.value ORDER BY ip.value), '{}')
  FROM unnest(ARRAY[
    'src_ip', 'src_ips', 'source_ip', 'source_ips', 'srcip',
    'dst_ip', 'dst_ips', 'dest_ip', 'dest_ips', 'destination_ip',
    'destination_ips', 'dstip', 'ip', 'ips', 'ip_address',
    'ip_addresses', 'local_ip', 'remote_ip', 'client_ip'
  ]) AS k(key)
  CROSS JOIN LATERAL (
    SELECT entity_context ->> k.key
    WHERE jsonb_typeof(entity_context -> k.key) = 'string'
    UNION ALL
    SELECT e #>> '{}'
    FROM jsonb_array_elements(
      CASE WHEN jsonb_typeof(entity_context -> k.key) = 'array'
        THEN entity_context -> k.key END
    ) AS e
    WHERE jsonb_typeof(e) = 'string'
  ) AS ip(value);
$$;

-- create_all runs everything above this line (core/storage/models/finding.py).

-- Stored: Postgres fills it for every existing row now and keeps it current
-- on every write. EXECUTE stays with PUBLIC, so vigil_app can call it.
ALTER TABLE public.findings
  ADD COLUMN ip_addresses text[] GENERATED ALWAYS AS (public.finding_ips(entity_context)) STORED;

CREATE INDEX idx_finding_ip_addresses ON public.findings USING gin (ip_addresses);
