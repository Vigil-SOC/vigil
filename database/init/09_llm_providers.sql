-- LLM Provider Configuration Tables
-- Tracks multi-provider LLM configuration (Anthropic, OpenAI, Ollama, ...).
-- API keys are NOT stored here; api_key_ref points to a secrets_manager key.

-- ============================================================================
-- LLM Provider Configs Table
-- ============================================================================
CREATE TABLE IF NOT EXISTS llm_provider_configs (
    provider_id VARCHAR(64) PRIMARY KEY,
    provider_type VARCHAR(32) NOT NULL,
    name VARCHAR(200) NOT NULL,
    base_url VARCHAR(500),
    api_key_ref VARCHAR(200),
    default_model VARCHAR(200) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_test_at TIMESTAMP,
    last_test_success BOOLEAN,
    last_error TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_llm_provider_type ON llm_provider_configs(provider_type);
CREATE INDEX IF NOT EXISTS idx_llm_provider_active ON llm_provider_configs(is_active);

-- Only one default provider per provider_type
CREATE UNIQUE INDEX IF NOT EXISTS llm_provider_default_per_type
    ON llm_provider_configs(provider_type)
    WHERE is_default = TRUE;

-- Update timestamp trigger
CREATE OR REPLACE FUNCTION update_llm_provider_configs_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_llm_provider_configs_updated_at ON llm_provider_configs;
CREATE TRIGGER trigger_llm_provider_configs_updated_at
    BEFORE UPDATE ON llm_provider_configs
    FOR EACH ROW
    EXECUTE FUNCTION update_llm_provider_configs_timestamp();

COMMENT ON TABLE llm_provider_configs IS 'LLM provider configuration (non-sensitive; keys in secrets_manager)';
COMMENT ON COLUMN llm_provider_configs.provider_type IS 'anthropic | openai | ollama';
COMMENT ON COLUMN llm_provider_configs.api_key_ref IS 'Secret name in secrets_manager (never the key itself)';
COMMENT ON COLUMN llm_provider_configs.config IS 'Provider-specific extras (e.g., openai organization, ollama pull policy)';

-- ============================================================================
-- Seed provider skeleton rows (both inactive, neither default).
-- The operator activates and sets a default via Settings → AI / LLM Providers
-- after supplying API keys. No provider is assumed here so fresh deployments
-- with only an Anthropic key, only an OpenAI key, or an Ollama endpoint all
-- start from the same neutral state.
-- ============================================================================
INSERT INTO llm_provider_configs (
    provider_id, provider_type, name, base_url, api_key_ref,
    default_model, is_active, is_default, config
) VALUES (
    'openai-default',
    'openai',
    'OpenAI',
    'https://api.openai.com/v1',
    'OPENAI_API_KEY',
    'gpt-4o',
    FALSE,
    FALSE,
    '{}'::jsonb
) ON CONFLICT (provider_id) DO NOTHING;

INSERT INTO llm_provider_configs (
    provider_id, provider_type, name, base_url, api_key_ref,
    default_model, is_active, is_default, config
) VALUES (
    'anthropic-default',
    'anthropic',
    'Anthropic',
    NULL,
    'CLAUDE_API_KEY',
    'claude-sonnet-4-6',
    FALSE,
    FALSE,
    '{}'::jsonb
) ON CONFLICT (provider_id) DO NOTHING;

-- ============================================================================
-- Grant Permissions
-- ============================================================================
GRANT SELECT, INSERT, UPDATE, DELETE ON llm_provider_configs TO deeptempo;
