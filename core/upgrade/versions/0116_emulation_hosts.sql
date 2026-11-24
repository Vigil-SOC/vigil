-- One host list for every emulator (#5061). The hosts stored on the Atomic Red
-- Team integration carry over into the `emulation` row, parsed as its tool did:
-- split on commas and newlines, trimmed, and an entry with whitespace or a
-- leading '-' dropped. Only when no `emulation` row exists: one a person already
-- saved wins. Then the integration's own field goes.
INSERT INTO public.system_config (key, value, description, config_type, updated_by)
SELECT 'emulation',
       jsonb_build_object(
           'tools', '["atomic-red-team_atomic_red_team_execute", "atomic_red_team_execute"]'::jsonb,
           'hosts', hosts
       ),
       'Emulator tools and the hosts they may run on',
       'autonomy',
       'system'
FROM (
    SELECT coalesce(jsonb_agg(host ORDER BY ord), '[]'::jsonb) AS hosts
    FROM (
        SELECT DISTINCT ON (lower(host)) host, ord
        FROM (
            SELECT regexp_replace(entry, '^\s+|\s+$', '', 'g') AS host, ord
            FROM public.integration_configs AS c,
                 regexp_split_to_table(coalesce(c.config->>'allowed_hosts', ''), '[,\n]')
                     WITH ORDINALITY AS t (entry, ord)
            WHERE c.integration_id = 'atomic-red-team'
        ) AS split
        WHERE host <> '' AND host !~ '\s' AND left(host, 1) <> '-'
        ORDER BY lower(host), ord
    ) AS kept
) AS carried
WHERE jsonb_array_length(hosts) > 0
ON CONFLICT (key) DO NOTHING;

UPDATE public.integration_configs
SET config = config - 'allowed_hosts'
WHERE integration_id = 'atomic-red-team' AND config ? 'allowed_hosts';
