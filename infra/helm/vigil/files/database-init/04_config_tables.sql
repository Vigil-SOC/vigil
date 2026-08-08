-- Configuration Management Tables
-- Migrates configuration from JSON files to PostgreSQL for better multi-user support

-- ============================================================================
-- System Configuration Table
-- ============================================================================
-- Stores system-wide configuration settings that apply to all users
CREATE TABLE IF NOT EXISTS system_config (
    key VARCHAR(100) PRIMARY KEY,
    value JSONB NOT NULL,
    description TEXT,
    config_type VARCHAR(50) NOT NULL DEFAULT 'general',
    updated_by VARCHAR(100),
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Indexes for system config
CREATE INDEX IF NOT EXISTS idx_system_config_type ON system_config(config_type);
CREATE INDEX IF NOT EXISTS idx_system_config_updated_at ON system_config(updated_at);

-- Add trigger to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_system_config_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_system_config_updated_at
    BEFORE UPDATE ON system_config
    FOR EACH ROW
    EXECUTE FUNCTION update_system_config_timestamp();

COMMENT ON TABLE system_config IS 'System-wide configuration settings';
COMMENT ON COLUMN system_config.key IS 'Configuration key (e.g., approval.force_manual_approval)';
COMMENT ON COLUMN system_config.value IS 'Configuration value as JSONB';
COMMENT ON COLUMN system_config.config_type IS 'Type/category of configuration (general, approval, theme, etc.)';


-- ============================================================================
-- User Preferences Table
-- ============================================================================
-- Stores per-user preferences and settings for multi-user deployments
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id VARCHAR(100) PRIMARY KEY,
    preferences JSONB NOT NULL DEFAULT '{}',
    display_name VARCHAR(200),
    email VARCHAR(200),
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    last_login TIMESTAMP
);

-- Add trigger to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_user_preferences_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_user_preferences_updated_at
    BEFORE UPDATE ON user_preferences
    FOR EACH ROW
    EXECUTE FUNCTION update_user_preferences_timestamp();

COMMENT ON TABLE user_preferences IS 'Per-user preferences and settings';
COMMENT ON COLUMN user_preferences.preferences IS 'User preferences as JSONB (theme, notifications, etc.)';


-- ============================================================================
-- Integration Configuration Table
-- ============================================================================
-- Stores non-sensitive integration settings (secrets remain in secrets_manager)
CREATE TABLE IF NOT EXISTS integration_configs (
    integration_id VARCHAR(100) PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT false,
    config JSONB NOT NULL DEFAULT '{}',
    integration_name VARCHAR(200),
    integration_type VARCHAR(50),
    description TEXT,
    last_test_at TIMESTAMP,
    last_test_success BOOLEAN,
    last_error TEXT,
    updated_by VARCHAR(100),
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Indexes for integration configs
CREATE INDEX IF NOT EXISTS idx_integration_enabled ON integration_configs(enabled);
CREATE INDEX IF NOT EXISTS idx_integration_type ON integration_configs(integration_type);
CREATE INDEX IF NOT EXISTS idx_integration_updated_at ON integration_configs(updated_at);

-- Add trigger to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_integration_configs_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_integration_configs_updated_at
    BEFORE UPDATE ON integration_configs
    FOR EACH ROW
    EXECUTE FUNCTION update_integration_configs_timestamp();

COMMENT ON TABLE integration_configs IS 'Integration configuration (non-sensitive settings only)';
COMMENT ON COLUMN integration_configs.config IS 'Integration-specific configuration as JSONB';
COMMENT ON COLUMN integration_configs.last_test_at IS 'Last time integration was tested';


-- ============================================================================
-- Configuration Audit Log Table
-- ============================================================================
-- Tracks all configuration changes for compliance and troubleshooting
CREATE TABLE IF NOT EXISTS config_audit_log (
    id SERIAL PRIMARY KEY,
    config_type VARCHAR(50) NOT NULL,
    config_key VARCHAR(200) NOT NULL,
    action VARCHAR(20) NOT NULL,
    old_value JSONB,
    new_value JSONB,
    changed_by VARCHAR(100) NOT NULL,
    change_reason TEXT,
    timestamp TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Indexes for audit log
CREATE INDEX IF NOT EXISTS idx_audit_config_type ON config_audit_log(config_type);
CREATE INDEX IF NOT EXISTS idx_audit_config_key ON config_audit_log(config_key);
CREATE INDEX IF NOT EXISTS idx_audit_changed_by ON config_audit_log(changed_by);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON config_audit_log(timestamp);

COMMENT ON TABLE config_audit_log IS 'Audit trail of all configuration changes';
COMMENT ON COLUMN config_audit_log.action IS 'Type of change: create, update, or delete';


-- ============================================================================
-- Insert Default System Configurations
-- ============================================================================
-- Migrate from file-based defaults

-- General configuration defaults
INSERT INTO system_config (key, value, description, config_type) 
VALUES (
    'general.settings',
    '{"auto_start_sync": false, "show_notifications": true, "theme": "dark", "enable_keyring": false}',
    'General application settings',
    'general'
) ON CONFLICT (key) DO NOTHING;

-- Approval workflow defaults
INSERT INTO system_config (key, value, description, config_type)
VALUES (
    'approval.force_manual_approval',
    '{"enabled": false}',
    'Force manual approval for all actions regardless of confidence score',
    'approval'
) ON CONFLICT (key) DO NOTHING;

-- Theme defaults
INSERT INTO system_config (key, value, description, config_type)
VALUES (
    'theme.current',
    '{"theme": "dark"}',
    'Current UI theme',
    'theme'
) ON CONFLICT (key) DO NOTHING;

-- Default user for single-user deployments
INSERT INTO user_preferences (user_id, preferences, display_name)
VALUES (
    'default',
    '{"theme": "dark", "show_notifications": true, "auto_start_sync": false}',
    'Default User'
) ON CONFLICT (user_id) DO NOTHING;


-- ============================================================================
-- Helper Views
-- ============================================================================

-- View of enabled integrations
CREATE OR REPLACE VIEW enabled_integrations AS
SELECT 
    integration_id,
    integration_name,
    integration_type,
    config,
    last_test_at,
    last_test_success,
    updated_at
FROM integration_configs
WHERE enabled = true
ORDER BY integration_name;

COMMENT ON VIEW enabled_integrations IS 'Quick view of all enabled integrations';

-- View of recent config changes
CREATE OR REPLACE VIEW recent_config_changes AS
SELECT 
    config_type,
    config_key,
    action,
    changed_by,
    timestamp,
    change_reason
FROM config_audit_log
ORDER BY timestamp DESC
LIMIT 100;

COMMENT ON VIEW recent_config_changes IS 'Most recent 100 configuration changes';


-- ============================================================================
-- Grant Permissions
-- ============================================================================
GRANT SELECT, INSERT, UPDATE, DELETE ON system_config TO deeptempo;
GRANT SELECT, INSERT, UPDATE, DELETE ON user_preferences TO deeptempo;
GRANT SELECT, INSERT, UPDATE, DELETE ON integration_configs TO deeptempo;
GRANT SELECT, INSERT ON config_audit_log TO deeptempo;
GRANT USAGE, SELECT ON SEQUENCE config_audit_log_id_seq TO deeptempo;
GRANT SELECT ON enabled_integrations TO deeptempo;
GRANT SELECT ON recent_config_changes TO deeptempo;

