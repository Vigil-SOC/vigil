#!/usr/bin/env python3
"""
Initialize default credentials in the database.

This script ensures that default roles and admin user are created in the database,
allowing first-time login with admin/admin123.

Can be run multiple times safely (idempotent operation).
"""

import sys
import os
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from database.connection import get_db_manager
from backend.services.auth_service import AuthService
from sqlalchemy import text
import logging

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)


def init_default_credentials():
    """Initialize default roles and admin user in the database."""
    
    print("=" * 50)
    print("Vigil SOC - Default Credentials Setup")
    print("=" * 50)
    
    # Initialize database connection
    try:
        db_manager = get_db_manager()
        db_manager.initialize()
        
        if not db_manager.health_check():
            print("❌ Database connection failed")
            print("   Make sure PostgreSQL is running:")
            print("   ./scripts/start_database.sh")
            return False

        print("✓ Database connection established")
    except Exception as e:
        print(f"❌ Failed to connect to database: {e}")
        print("   Make sure PostgreSQL is running:")
        print("   ./scripts/start_database.sh")
        return False
    
    # SQL to create tables if they don't exist
    create_tables_sql = """
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
        created_at TIMESTAMP NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMP NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_user_username ON users(username);
    CREATE INDEX IF NOT EXISTS idx_user_email ON users(email);
    CREATE INDEX IF NOT EXISTS idx_user_role_id ON users(role_id);
    CREATE INDEX IF NOT EXISTS idx_user_is_active ON users(is_active);
    """
    
    # SQL to insert default roles
    roles_sql = """
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
    """
    
    # SQL to insert default admin user
    # Password: admin123
    # Hash: $2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5aeWG6QErKLzG
    admin_user_sql = """
    INSERT INTO users (user_id, username, email, password_hash, full_name, role_id, is_active, is_verified) VALUES
    ('user-admin-default', 'admin', 'admin@deeptempo.ai', '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5aeWG6QErKLzG', 'System Administrator', 'role-admin', true, true)
    ON CONFLICT (user_id) DO NOTHING;
    """
    
    try:
        with db_manager.session_scope() as session:
            # Create tables
            session.execute(text(create_tables_sql))
            print("✓ Roles table ready")
            print("✓ Users table ready")
            
            # Insert default roles
            session.execute(text(roles_sql))
            
            # Count roles
            result = session.execute(text("SELECT COUNT(*) FROM roles"))
            count = result.scalar()
            print(f"✓ Default roles inserted ({count} roles)")
            
            # Insert default admin user
            session.execute(text(admin_user_sql))
            
            # Check if admin user exists
            result = session.execute(text("SELECT username, email, full_name FROM users WHERE username = 'admin'"))
            admin = result.fetchone()
            
            if admin:
                print("✓ Default admin user created")
            else:
                print("⚠️  Admin user may already exist")
        
        # Verify authentication
        print("")
        print("Verifying authentication...")
        user = AuthService.authenticate_user("admin", "admin123")
        
        if user:
            print("✓ Authentication verified")
            print("")
            print("=" * 50)
            print("✓ Default Credentials Ready!")
            print("=" * 50)
            print(f"Username: admin")
            print(f"Password: admin123")
            print(f"Email:    admin@deeptempo.ai")
            print(f"Role:     Admin (full system access)")
            print("")
            print("⚠️  IMPORTANT: Change the default password after first login!")
            print("")
            
            # List all available roles
            with db_manager.session_scope() as session:
                result = session.execute(text("SELECT role_id, name, description FROM roles ORDER BY role_id"))
                roles = result.fetchall()
                print("Available roles:")
                for role in roles:
                    print(f"  - {role[1]}: {role[2]}")
            
            return True
        else:
            print("❌ Authentication verification failed")
            print("   The admin user was created but login doesn't work")
            print("   Check database logs for more information")
            return False
    
    except Exception as e:
        print(f"❌ Failed to initialize credentials: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = init_default_credentials()
    sys.exit(0 if success else 1)

