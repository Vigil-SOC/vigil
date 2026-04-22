#!/usr/bin/env python3
"""
Initialize default admin user in the database.

This script ensures that a default admin user exists on first launch.
Default credentials: admin / admin123 (CHANGE IN PRODUCTION!)
"""

import sys
import os
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from database.connection import get_db_manager
from sqlalchemy import text
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def init_default_user():
    """Initialize default admin user in the database."""
    db_manager = get_db_manager()
    db_manager.initialize()
    
    # Default admin credentials
    DEFAULT_USERNAME = "admin"
    DEFAULT_PASSWORD_HASH = "$2b$12$.5ZQKm.dZ5kYyf65VyvZc.CDKNyR5OXoXD7nIvCjbXWOemYIZ8/Xe"  # admin123
    DEFAULT_EMAIL = "admin@deeptempo.ai"
    DEFAULT_FULL_NAME = "System Administrator"
    DEFAULT_USER_ID = "user-admin-default"
    DEFAULT_ROLE_ID = "role-admin"
    
    try:
        with db_manager.session_scope() as session:
            # First ensure roles table and default roles exist
            logger.info("Ensuring default roles exist...")
            
            # Create roles table if it doesn't exist
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS roles (
                    role_id VARCHAR(50) PRIMARY KEY,
                    name VARCHAR(100) UNIQUE NOT NULL,
                    description TEXT NOT NULL,
                    permissions JSONB NOT NULL DEFAULT '{}',
                    is_system_role BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """))
            
            # Insert admin role
            session.execute(text("""
                INSERT INTO roles (role_id, name, description, permissions, is_system_role) VALUES
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
                ON CONFLICT (role_id) DO NOTHING
            """))
            
            # Create users table if it doesn't exist
            logger.info("Ensuring users table exists...")
            session.execute(text("""
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
                    password_history JSONB NOT NULL DEFAULT '[]',
                    password_changed_at TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """))

            # Reconcile older deployed schemas: add missing columns and (re)assert
            # defaults on columns that may have been created without them.
            # All statements are idempotent and safe on fresh DBs.
            logger.info("Reconciling users table schema with latest definition...")
            session.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_login_count INTEGER NOT NULL DEFAULT 0"
            ))
            session.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMP"
            ))
            session.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_history JSONB NOT NULL DEFAULT '[]'"
            ))
            session.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMP"
            ))
            session.execute(text(
                "ALTER TABLE users ALTER COLUMN mfa_enabled SET DEFAULT FALSE"
            ))
            session.execute(text(
                "ALTER TABLE users ALTER COLUMN login_count SET DEFAULT 0"
            ))
            
            # Check if default admin user already exists
            result = session.execute(
                text("SELECT COUNT(*) FROM users WHERE username = :username"),
                {"username": DEFAULT_USERNAME}
            )
            count = result.scalar()
            
            if count > 0:
                logger.info(f"✓ Default admin user already exists: {DEFAULT_USERNAME}")
                return
            
            # Insert default admin user. Explicit values for every NOT NULL
            # column guard against stale deployed schemas where a column may
            # exist without its server-side default.
            logger.info(f"Creating default admin user: {DEFAULT_USERNAME}")
            session.execute(text("""
                INSERT INTO users (
                    user_id, username, email, password_hash, full_name, role_id,
                    is_active, is_verified,
                    mfa_enabled, login_count, failed_login_count, password_history
                )
                VALUES (
                    :user_id, :username, :email, :password_hash, :full_name, :role_id,
                    true, true,
                    false, 0, 0, '[]'::jsonb
                )
                ON CONFLICT (user_id) DO NOTHING
            """), {
                "user_id": DEFAULT_USER_ID,
                "username": DEFAULT_USERNAME,
                "email": DEFAULT_EMAIL,
                "password_hash": DEFAULT_PASSWORD_HASH,
                "full_name": DEFAULT_FULL_NAME,
                "role_id": DEFAULT_ROLE_ID
            })
            
            # Verify user was created
            result = session.execute(
                text("SELECT username, email, full_name FROM users WHERE username = :username"),
                {"username": DEFAULT_USERNAME}
            )
            user = result.fetchone()
            
            if user:
                logger.info("=" * 60)
                logger.info("✅ Default admin user created successfully!")
                logger.info("=" * 60)
                logger.info(f"   Username:  {user[0]}")
                logger.info(f"   Email:     {user[1]}")
                logger.info(f"   Password:  admin123")
                logger.info(f"   Full Name: {user[2]}")
                logger.info("=" * 60)
                logger.info("⚠️  IMPORTANT: Change the default password in production!")
                logger.info("=" * 60)
            else:
                logger.warning("⚠️  User creation may have been skipped (already exists)")
    
    except Exception as e:
        logger.error(f"❌ Failed to initialize default user: {e}")
        logger.error("   This usually means PostgreSQL is not running or not accessible.")
        logger.error("   Make sure Docker is running and PostgreSQL container is healthy:")
        logger.error("     docker ps | grep deeptempo-postgres")
        logger.error("     docker exec deeptempo-postgres pg_isready -U deeptempo -d deeptempo_soc")
        # Don't raise - this should be non-fatal to allow startup to continue
        logger.warning("Continuing startup despite user initialization error...")


if __name__ == "__main__":
    init_default_user()

