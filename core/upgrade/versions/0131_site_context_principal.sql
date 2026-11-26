-- The site context built-in (AgentId.SITE_CONTEXT), seeded as 0003 seeded the others.
INSERT INTO public.principals (principal_id, kind, label) VALUES
    ('agent:site_context', 'agent', 'Site context agent')
ON CONFLICT (principal_id) DO NOTHING;
