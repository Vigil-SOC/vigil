#!/usr/bin/env python3
"""
Schema migration script for Vigil SOC.

Brings an existing database up to date with the current SQLAlchemy models
defined in core.storage.models. Safe to run multiple times (idempotent).

Usage:
    python scripts/migrate_schema.py
    # or with a custom connection string:
    DATABASE_URL="postgresql://user:pass@host:5432/db" python scripts/migrate_schema.py
"""

import os
import sys
from pathlib import Path
from urllib.parse import quote

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
    return (
        f'postgresql://{quote(user, safe="")}:{quote(pw, safe="")}'
        f'@{host}:{port}/{db}'
    )


MIGRATIONS = []

def _table_exists(conn, name):
    """Whether a table is there to be altered.

    Column migrations run after create_all has made any missing tables, but a
    table can still be absent -- an older database that predates it, a partial
    restore. ALTER on a missing table aborts the transaction all of these share,
    so every step after it fails too, reporting a schema problem that is not
    there.
    """
    return conn.execute(
        text("SELECT to_regclass(:name)"), {"name": name}
    ).scalar() is not None


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
# llm_interaction_logs table
# ---------------------------------------------------------------------------

# Bifrost virtual-key attribution (#186). The column is declared on the ORM
# model but create_all() does not ALTER existing tables, so databases
# initialized before #186 landed are missing the column and every
# /api/reasoning/* read returns 500.
@migration("Add virtual_key_id column to llm_interaction_logs")
def add_llm_interaction_virtual_key_id(conn):
    conn.execute(text("""
        ALTER TABLE llm_interaction_logs
        ADD COLUMN IF NOT EXISTS virtual_key_id VARCHAR(64);
    """))

@migration("Create idx_llm_interaction_vk index")
def create_llm_interaction_vk_index(conn):
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_llm_interaction_vk
        ON llm_interaction_logs (virtual_key_id, created_at);
    """))

# Unpriced is stored as NULL, not 0 (#1115). Existing rows are left as they are.
@migration("Make llm_interaction_logs.cost_usd nullable")
def make_llm_interaction_cost_nullable(conn):
    if not _table_exists(conn, 'llm_interaction_logs'):
        return
    conn.execute(text("""
        ALTER TABLE llm_interaction_logs ALTER COLUMN cost_usd DROP NOT NULL;
    """))
    conn.execute(text("""
        ALTER TABLE llm_interaction_logs ALTER COLUMN cost_usd DROP DEFAULT;
    """))


# Rates behind cost_usd, frozen when the row is written (#1190). DOUBLE PRECISION
# because Numeric(10, 6) — the call total's scale — rounds a per-token cache
# rate below 1e-6 away to zero.
@migration("Add rate columns to llm_interaction_logs")
def add_llm_interaction_rate_columns(conn):
    if not _table_exists(conn, 'llm_interaction_logs'):
        return
    conn.execute(text("""
        ALTER TABLE llm_interaction_logs
            ADD COLUMN IF NOT EXISTS input_cost_per_token DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS output_cost_per_token DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS cache_read_cost_per_token DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS cache_write_cost_per_token DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS rates_fetched_at VARCHAR(64);
    """))


# create_all is checkfirst=True, so a table that already exists gets no new index
# from the model. A hunt handing off looks this column up twice per escalation.
@migration("Create idx_workflow_runs_triggered_by index")
def create_workflow_runs_triggered_by_index(conn):
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_workflow_runs_triggered_by
        ON workflow_runs (triggered_by, started_at);
    """))


# ---------------------------------------------------------------------------
# New tables (create if missing via SQLAlchemy create_all)
# ---------------------------------------------------------------------------

@migration("Create any missing tables from models")
def create_missing_tables(conn):
    from core.storage.models import Base
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
# Episodic memory
# ---------------------------------------------------------------------------

# Which kind of actor closed a Case (#733). Read as a Verdict's Trust, and a
# name cannot answer it -- an agent closing as "soc-automation" and a person
# closing as "nestor" are the same shape of string. Existing rows default to
# `agent`: `analyst` is the highest-trust record the system produces, and a
# close nobody can attribute has not earned it.
@migration("Add closed_by_kind column to case_closure_info")
def add_case_closure_actor(conn):
    if not _table_exists(conn, 'case_closure_info'):
        return
    conn.execute(text("""
        ALTER TABLE case_closure_info
        ADD COLUMN IF NOT EXISTS closed_by_kind TEXT NOT NULL DEFAULT 'agent';
    """))
    conn.execute(text("""
        ALTER TABLE case_closure_info
        DROP CONSTRAINT IF EXISTS case_closure_info_closed_by_kind_check;
    """))
    conn.execute(text("""
        ALTER TABLE case_closure_info
        ADD CONSTRAINT case_closure_info_closed_by_kind_check
            CHECK (closed_by_kind IN ('analyst', 'agent'));
    """))


# The runs a marker accounts for (#731), and the origin pair a Case has no value
# for (#733). A Case is closed and never run, so its marker's origin is absent;
# the CHECK ties that absence to the kind, so neither shape can be half-written.
@migration("Widen episodic_distil_markers for Case-authored Verdicts")
def widen_episodic_distil_markers(conn):
    if not _table_exists(conn, 'episodic_distil_markers'):
        return
    conn.execute(text("""
        ALTER TABLE episodic_distil_markers
        ADD COLUMN IF NOT EXISTS origin_run_ids UUID[] NOT NULL
            DEFAULT ARRAY[]::uuid[];
    """))
    # Dropped first, not IF NOT EXISTS: a database created before #731 already
    # holds an index of this name over origin_run_id, and IF NOT EXISTS would
    # see the name taken and leave the poll's `@>` containment unindexed.
    conn.execute(text("DROP INDEX IF EXISTS idx_episodic_markers_origin;"))
    conn.execute(text("""
        CREATE INDEX idx_episodic_markers_origin
        ON episodic_distil_markers USING GIN (origin_run_ids);
    """))
    conn.execute(text("""
        ALTER TABLE episodic_distil_markers
        ALTER COLUMN origin_run_id DROP NOT NULL;
    """))
    conn.execute(text("""
        ALTER TABLE episodic_distil_markers
        ALTER COLUMN origin_seq DROP NOT NULL;
    """))
    conn.execute(text("""
        ALTER TABLE episodic_distil_markers
        DROP CONSTRAINT IF EXISTS episodic_distil_markers_origin_matches_kind;
    """))
    conn.execute(text("""
        ALTER TABLE episodic_distil_markers
        ADD CONSTRAINT episodic_distil_markers_origin_matches_kind CHECK (
            (investigation_kind = 'hunt') = (origin_run_id IS NOT NULL)
            AND (origin_run_id IS NULL) = (origin_seq IS NULL)
        );
    """))


# ---------------------------------------------------------------------------
# intake_triggers table
# ---------------------------------------------------------------------------

# The Case the row was claimed under (#1000). #918 created this table before the
# column existed, so every database that drained an intake queue between the two
# has the table without it -- and create_all never alters one it finds. The whole
# row is selected on every drain, so the missing column fails the queue read
# rather than one launch: the daemon reports an empty queue and launches nothing.
@migration("Add case_id column to intake_triggers")
def add_intake_trigger_case_id(conn):
    if not _table_exists(conn, 'intake_triggers'):
        return
    conn.execute(text("""
        ALTER TABLE intake_triggers
        ADD COLUMN IF NOT EXISTS case_id VARCHAR(50);
    """))


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
            VALUES (:role_id, :name, :desc, CAST(:perms AS jsonb), :is_sys, now(), now())
            ON CONFLICT (role_id) DO NOTHING
        """), {"role_id": role_id, "name": name, "desc": description,
               "perms": permissions, "is_sys": is_system})
    logger.info("  Seeded default roles: admin, analyst, viewer")


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
