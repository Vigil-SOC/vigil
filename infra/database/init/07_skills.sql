-- Skills: reusable, parameterized SOC capabilities (detection, enrichment,
-- response, reporting). Agents and workflows will compose skills in later PRs;
-- this migration establishes the table that backs Issue #82's MVP.

CREATE TABLE IF NOT EXISTS skills (
    skill_id VARCHAR(32) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    category VARCHAR(32) NOT NULL
        CHECK (category IN ('detection','enrichment','response','reporting','custom')),
    input_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    required_tools JSONB NOT NULL DEFAULT '[]'::jsonb,
    prompt_template TEXT NOT NULL,
    execution_steps JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_by VARCHAR(255),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_skill_category  ON skills(category);
CREATE INDEX IF NOT EXISTS idx_skill_is_active ON skills(is_active);
CREATE INDEX IF NOT EXISTS idx_skill_name_trgm ON skills USING gin (name gin_trgm_ops);
