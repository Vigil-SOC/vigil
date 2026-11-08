-- Operator agents are plugin manifests (#4023). The revision beside this file
-- fills the column for every existing row; the persona columns stay until #4024.

ALTER TABLE public.custom_agents ADD COLUMN manifest text;

COMMENT ON COLUMN public.custom_agents.manifest IS
    'Operator plugin manifest (YAML) a run of this agent loads.';
