-- The Amazon Security Lake tool server's one name (#4701), classed as 0017 classed
-- every other in-repo server's reads. A row a person already set wins.
INSERT INTO public.tool_effect_classes (tool_name, effect_class, classified_by)
SELECT tool_name, effect_class, 'system'
FROM (VALUES
    ('amazon-security-lake_security_lake_query', 'read_unverified')
) AS seed (tool_name, effect_class)
ON CONFLICT (tool_name) DO NOTHING;
