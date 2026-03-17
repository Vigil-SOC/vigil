#!/usr/bin/env python3
"""
Schema migration script for Vigil SOC.

Brings an existing database up to date with the current SQLAlchemy models
defined in database/models.py. Safe to run multiple times (idempotent).

Usage:
    python scripts/migrate_schema.py
    # or with a custom connection string:
    DATABASE_URL="postgresql://user:pass@host:5432/db" python scripts/migrate_schema.py
"""

import os
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

import logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

from sqlalchemy import create_engine, text, inspect

def get_connection_url():
    url = os.environ.get('DATABASE_URL')
    if url:
        return url
    env_file = Path.home() / '.deeptempo' / '.env'
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith('DATABASE_URL='):
                return line.split('=', 1)[1].strip().strip('"').strip("'")
    host = os.environ.get('POSTGRES_HOST', 'localhost')
    port = os.environ.get('POSTGRES_PORT', '5432')
    user = os.environ.get('POSTGRES_USER', 'deeptempo')
    pw = os.environ.get('POSTGRES_PASSWORD', 'deeptempo_secure_password_change_me')
    db = os.environ.get('POSTGRES_DB', 'deeptempo_soc')
    return f'postgresql://{user}:{pw}@{host}:{port}/{db}'


MIGRATIONS = []

def migration(description):
    """Decorator to register a migration step."""
    def decorator(fn):
        MIGRATIONS.append((description, fn))
        return fn
    return decorator


# ---------------------------------------------------------------------------
# Extensions
# ---------------------------------------------------------------------------

@migration("Enable pg_trgm extension")
def enable_pg_trgm(conn):
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm;"))


@migration("Enable uuid-ossp extension")
def enable_uuid_ossp(conn):
    conn.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";'))


# ---------------------------------------------------------------------------
# findings table
# ---------------------------------------------------------------------------

@migration("Add description column to findings")
def add_findings_description(conn):
    conn.execute(text("""
        ALTER TABLE findings ADD COLUMN IF NOT EXISTS description TEXT;
    """))

@migration("Fix findings.created_at server default to now()")
def fix_findings_created_at(conn):
    conn.execute(text("""
        ALTER TABLE findings ALTER COLUMN created_at SET DEFAULT now();
    """))

@migration("Fix findings.updated_at server default to now()")
def fix_findings_updated_at(conn):
    conn.execute(text("""
        ALTER TABLE findings ALTER COLUMN updated_at SET DEFAULT now();
    """))

@migration("Create GIN trigram index on findings.description")
def create_findings_description_gin_index(conn):
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_finding_description
        ON findings USING gin (description gin_trgm_ops);
    """))


# ---------------------------------------------------------------------------
# cases table
# ---------------------------------------------------------------------------

@migration("Fix cases.created_at server default to now()")
def fix_cases_created_at(conn):
    conn.execute(text("""
        ALTER TABLE cases ALTER COLUMN created_at SET DEFAULT now();
    """))

@migration("Fix cases.updated_at server default to now()")
def fix_cases_updated_at(conn):
    conn.execute(text("""
        ALTER TABLE cases ALTER COLUMN updated_at SET DEFAULT now();
    """))


# ---------------------------------------------------------------------------
# New tables (create if missing via SQLAlchemy create_all)
# ---------------------------------------------------------------------------

@migration("Create any missing tables from models")
def create_missing_tables(conn):
    from database.models import Base
    engine = conn.engine if hasattr(conn, 'engine') else conn
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    model_tables = set(Base.metadata.tables.keys())
    missing = model_tables - existing
    if missing:
        logger.info(f"  Creating missing tables: {', '.join(sorted(missing))}")
        Base.metadata.create_all(engine, tables=[
            Base.metadata.tables[t] for t in missing
        ])
    else:
        logger.info("  All tables already exist")


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

@migration("Seed default roles if roles table is empty")
def seed_default_roles(conn):
    result = conn.execute(text("SELECT COUNT(*) FROM roles"))
    count = result.scalar()
    if count > 0:
        logger.info(f"  Roles table already has {count} rows, skipping seed")
        return

    import json
    roles = [
        ('admin', 'Administrator', 'Full system access',
         json.dumps({"admin": True, "manage_users": True, "manage_cases": True,
                      "manage_findings": True, "manage_settings": True,
                      "view_audit_logs": True}), True),
        ('analyst', 'Security Analyst', 'Can manage cases and findings',
         json.dumps({"manage_cases": True, "manage_findings": True,
                      "view_audit_logs": True}), True),
        ('viewer', 'Viewer', 'Read-only access',
         json.dumps({"view_cases": True, "view_findings": True}), True),
    ]
    for role_id, name, description, permissions, is_system in roles:
        conn.execute(text("""
            INSERT INTO roles (role_id, name, description, permissions, is_system_role, created_at, updated_at)
            VALUES (:role_id, :name, :desc, :perms::jsonb, :is_sys, now(), now())
            ON CONFLICT (role_id) DO NOTHING
        """), {"role_id": role_id, "name": name, "desc": description,
               "perms": permissions, "is_sys": is_system})
    logger.info("  Seeded default roles: admin, analyst, viewer")


@migration("Seed default admin user if users table is empty")
def seed_default_admin(conn):
    result = conn.execute(text("SELECT COUNT(*) FROM users"))
    count = result.scalar()
    if count > 0:
        logger.info(f"  Users table already has {count} users, skipping seed")
        return

    from passlib.hash import bcrypt
    pw_hash = bcrypt.hash("admin")
    conn.execute(text("""
        INSERT INTO users (user_id, username, email, password_hash, full_name, role_id,
                           is_active, is_verified, mfa_enabled, login_count, created_at, updated_at)
        VALUES ('user-admin', 'admin', 'admin@deeptempo.local', :pw, 'Administrator', 'admin',
                true, true, false, 0, now(), now())
        ON CONFLICT (user_id) DO NOTHING
    """), {"pw": pw_hash})
    logger.info("  Seeded default admin user (admin / admin)")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_migrations():
    url = get_connection_url()
    safe_url = url.split('@')[-1] if '@' in url else url
    logger.info(f"Connecting to: ...@{safe_url}")

    engine = create_engine(url)

    applied = 0
    errors = 0

    with engine.begin() as conn:
        for desc, fn in MIGRATIONS:
            try:
                logger.info(f"[{applied+1}/{len(MIGRATIONS)}] {desc}")
                fn(conn)
                applied += 1
            except Exception as e:
                logger.error(f"  FAILED: {e}")
                errors += 1

    logger.info(f"\nDone: {applied} applied, {errors} errors out of {len(MIGRATIONS)} migrations.")
    return errors == 0


if __name__ == '__main__':
    success = run_migrations()
    sys.exit(0 if success else 1)
