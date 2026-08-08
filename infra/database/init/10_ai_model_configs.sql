-- Per-Component AI Model Assignments (GH #89)
-- Maps system components (chat, triage, investigation, orchestrator_plan,
-- orchestrator_review, summarization, reporting) to a (provider, model) pair.
-- When a row is absent, the component falls back to `chat_default`.

CREATE TABLE IF NOT EXISTS ai_model_configs (
    component VARCHAR(64) PRIMARY KEY,
    provider_id VARCHAR(64) NOT NULL REFERENCES llm_provider_configs(provider_id) ON DELETE RESTRICT,
    model_id VARCHAR(200) NOT NULL,
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_by VARCHAR(64),
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ai_model_configs_provider ON ai_model_configs(provider_id);

CREATE OR REPLACE FUNCTION update_ai_model_configs_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_ai_model_configs_updated_at ON ai_model_configs;
CREATE TRIGGER trigger_ai_model_configs_updated_at
    BEFORE UPDATE ON ai_model_configs
    FOR EACH ROW
    EXECUTE FUNCTION update_ai_model_configs_timestamp();

COMMENT ON TABLE ai_model_configs IS 'Per-component AI model assignments (GH #89)';
COMMENT ON COLUMN ai_model_configs.component IS
    'chat_default | triage | investigation | orchestrator_plan | orchestrator_review | summarization | reporting';
COMMENT ON COLUMN ai_model_configs.settings IS
    'Component-specific overrides (max_tokens, thinking_budget, temperature)';

-- Seed chat_default from the existing default Anthropic provider so upgrades
-- behave identically to the previous hardcoded default.
INSERT INTO ai_model_configs (component, provider_id, model_id, settings)
SELECT 'chat_default', provider_id, default_model, '{}'::jsonb
FROM llm_provider_configs
WHERE provider_type = 'anthropic' AND is_default = TRUE
ON CONFLICT (component) DO NOTHING;

GRANT SELECT, INSERT, UPDATE, DELETE ON ai_model_configs TO deeptempo;
