-- Falcon's host search is a read: a run's proposal asks it whether a host is
-- already contained, classed like the containment lookups 0115 classed. A row a
-- person already set wins.
INSERT INTO public.tool_effect_classes (tool_name, effect_class, classified_by)
SELECT tool_name, effect_class, 'system'
FROM (VALUES
    ('crowdstrike_falcon_search_hosts', 'read_unverified')
) AS seed (tool_name, effect_class)
ON CONFLICT (tool_name) DO NOTHING;
