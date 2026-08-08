-- Add component_category to custom_agents (GH #89)
-- Lets custom agents inherit a per-component model assignment (triage,
-- investigation, reporting) from ai_model_configs when no per-agent
-- `model` override is set.

ALTER TABLE custom_agents
    ADD COLUMN IF NOT EXISTS component_category VARCHAR(32) NOT NULL DEFAULT 'investigation';

COMMENT ON COLUMN custom_agents.component_category IS
    'triage | investigation | reporting — maps to ai_model_configs components';
