-- Unblock is an act, classed like the block it undoes (#5234), as 0115 classed
-- the other undo tools. A row a person already set wins.
INSERT INTO public.tool_effect_classes (tool_name, effect_class, classified_by)
SELECT tool_name, effect_class, 'system'
FROM (VALUES
    ('cloudflare_cf_gateway_unblock_domain', 'irreversible')
) AS seed (tool_name, effect_class)
ON CONFLICT (tool_name) DO NOTHING;
