-- Authentication and Authorization Tables
-- User and Role Management for RBAC

-- Roles table
CREATE TABLE IF NOT EXISTS roles (
    role_id VARCHAR(50) PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT NOT NULL,
    permissions JSONB NOT NULL DEFAULT '{}',
    is_system_role BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_role_name ON roles(name);

-- Users table
CREATE TABLE IF NOT EXISTS users (
    user_id VARCHAR(50) PRIMARY KEY,
    username VARCHAR(100) UNIQUE NOT NULL,
    email VARCHAR(200) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(200) NOT NULL,
    role_id VARCHAR(50) NOT NULL REFERENCES roles(role_id),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    is_verified BOOLEAN NOT NULL DEFAULT FALSE,
    mfa_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    mfa_secret VARCHAR(255),
    last_login TIMESTAMP,
    login_count INTEGER NOT NULL DEFAULT 0,
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    locked_until TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Additive migration for existing deployments (safe to rerun)
ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_login_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_user_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_user_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_user_role_id ON users(role_id);
CREATE INDEX IF NOT EXISTS idx_user_is_active ON users(is_active);

-- Insert default roles
INSERT INTO roles (role_id, name, description, permissions, is_system_role) VALUES
('role-viewer', 'Viewer', 'Read-only access to findings and cases', '{
    "findings.read": true,
    "cases.read": true,
    "integrations.read": false,
    "users.read": false,
    "settings.read": false,
    "ai_chat.use": false,
    "ai_decisions.approve": false
}', true),
('role-analyst', 'Analyst', 'Full access to findings and cases, limited integrations', '{
    "findings.read": true,
    "findings.write": true,
    "findings.delete": false,
    "cases.read": true,
    "cases.write": true,
    "cases.delete": false,
    "cases.assign": false,
    "integrations.read": true,
    "integrations.write": false,
    "users.read": false,
    "settings.read": true,
    "settings.write": false,
    "ai_chat.use": true,
    "ai_decisions.approve": false
}', true),
('role-senior-analyst', 'Senior Analyst', 'Full analyst access plus approval rights', '{
    "findings.read": true,
    "findings.write": true,
    "findings.delete": true,
    "cases.read": true,
    "cases.write": true,
    "cases.delete": false,
    "cases.assign": true,
    "integrations.read": true,
    "integrations.write": true,
    "users.read": true,
    "settings.read": true,
    "settings.write": false,
    "ai_chat.use": true,
    "ai_decisions.approve": true
}', true),
('role-manager', 'Manager', 'User management and all integrations', '{
    "findings.read": true,
    "findings.write": true,
    "findings.delete": true,
    "cases.read": true,
    "cases.write": true,
    "cases.delete": true,
    "cases.assign": true,
    "integrations.read": true,
    "integrations.write": true,
    "users.read": true,
    "users.write": true,
    "users.delete": false,
    "settings.read": true,
    "settings.write": true,
    "ai_chat.use": true,
    "ai_decisions.approve": true
}', true),
('role-admin', 'Admin', 'Full system access', '{
    "findings.read": true,
    "findings.write": true,
    "findings.delete": true,
    "cases.read": true,
    "cases.write": true,
    "cases.delete": true,
    "cases.assign": true,
    "integrations.read": true,
    "integrations.write": true,
    "users.read": true,
    "users.write": true,
    "users.delete": true,
    "settings.read": true,
    "settings.write": true,
    "ai_chat.use": true,
    "ai_decisions.approve": true
}', true)
ON CONFLICT (role_id) DO NOTHING;

-- Create default admin user (password: admin123 - CHANGE IN PRODUCTION!)
-- Password hash for 'admin123' using bcrypt
INSERT INTO users (user_id, username, email, password_hash, full_name, role_id, is_active, is_verified) VALUES
('user-admin-default', 'admin', 'admin@deeptempo.ai', '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5aeWG6QErKLzG', 'System Administrator', 'role-admin', true, true)
ON CONFLICT (user_id) DO NOTHING;

-- Update trigger for updated_at
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_users_updated_at BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_roles_updated_at BEFORE UPDATE ON roles
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

