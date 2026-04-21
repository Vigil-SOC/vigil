-- Custom SOC Agents Table
-- Stores user-defined agents created via the Agent Builder UI.
-- Built-in agents remain hardcoded in services/soc_agents.py; this table
-- only holds custom agents. IDs are prefixed "custom-" to disambiguate.

CREATE TABLE IF NOT EXISTS custom_agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    icon TEXT,
    color TEXT,
    specialization TEXT,
    role TEXT NOT NULL,
    extra_principles TEXT NOT NULL DEFAULT '',
    methodology TEXT NOT NULL DEFAULT '',
    system_prompt_override TEXT,
    recommended_tools JSONB NOT NULL DEFAULT '[]'::jsonb,
    max_tokens INTEGER NOT NULL DEFAULT 4096,
    enable_thinking BOOLEAN NOT NULL DEFAULT FALSE,
    model TEXT,
    created_by VARCHAR(100),
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_custom_agents_updated_at ON custom_agents(updated_at DESC);

CREATE OR REPLACE FUNCTION update_custom_agents_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_custom_agents_updated_at ON custom_agents;
CREATE TRIGGER trigger_custom_agents_updated_at
    BEFORE UPDATE ON custom_agents
    FOR EACH ROW
    EXECUTE FUNCTION update_custom_agents_timestamp();

COMMENT ON TABLE custom_agents IS 'User-defined SOC agents created via the Agent Builder UI';
COMMENT ON COLUMN custom_agents.id IS 'Agent identifier, format "custom-<slug>"';
COMMENT ON COLUMN custom_agents.role IS 'Injected into BASE_PROMPT {role} placeholder';
COMMENT ON COLUMN custom_agents.extra_principles IS 'Injected into BASE_PROMPT {extra_principles} placeholder';
COMMENT ON COLUMN custom_agents.methodology IS 'Injected into BASE_PROMPT {methodology} placeholder';
COMMENT ON COLUMN custom_agents.system_prompt_override IS 'When set, bypasses BASE_PROMPT and uses this text verbatim';
COMMENT ON COLUMN custom_agents.recommended_tools IS 'Array of MCP tool names (full {server}_{tool} format)';
COMMENT ON COLUMN custom_agents.model IS 'Reserved for per-agent model override (issue #89); null = use caller default';

GRANT SELECT, INSERT, UPDATE, DELETE ON custom_agents TO deeptempo;
