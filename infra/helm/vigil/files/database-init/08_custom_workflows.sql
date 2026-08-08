-- Custom Workflows Tables
-- User-created, database-backed workflow definitions for the Workflow Builder feature.
-- File-based WORKFLOW.md definitions remain supported separately.

CREATE TABLE IF NOT EXISTS custom_workflows (
    workflow_id VARCHAR(100) PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    description TEXT NOT NULL,
    use_case TEXT,
    trigger_examples JSONB NOT NULL DEFAULT '[]'::jsonb,
    phases JSONB NOT NULL DEFAULT '[]'::jsonb,
    graph_layout JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_by VARCHAR(100),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_custom_workflows_active ON custom_workflows(is_active);
CREATE INDEX IF NOT EXISTS idx_custom_workflows_created_by ON custom_workflows(created_by);
CREATE INDEX IF NOT EXISTS idx_custom_workflows_name ON custom_workflows(name);
