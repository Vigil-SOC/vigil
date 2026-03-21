"""Migration: add canonical entity fields to findings table.

Adds 8 new columns (src_ip, dst_ip, hostname, username, process_name,
file_hash, alert_category, raw_fields) and creates indexes.

Safe to run multiple times — checks information_schema / pg_indexes before
each ALTER TABLE / CREATE INDEX so already-existing columns and indexes are
skipped rather than causing an error.

Usage:
    source venv/bin/activate
    python scripts/migrate_canonical_fields.py
"""

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

ADD_COLUMNS = [
    ("src_ip",         "VARCHAR(45)"),
    ("dst_ip",         "VARCHAR(45)"),
    ("hostname",       "VARCHAR(255)"),
    ("username",       "VARCHAR(255)"),
    ("process_name",   "VARCHAR(512)"),
    ("file_hash",      "VARCHAR(128)"),
    ("alert_category", "VARCHAR(64)"),
    ("raw_fields",     "JSONB"),
]

ADD_INDEXES = [
    ("idx_finding_src_ip",        "findings", "src_ip"),
    ("idx_finding_dst_ip",        "findings", "dst_ip"),
    ("idx_finding_hostname",      "findings", "hostname"),
    ("idx_finding_username",      "findings", "username"),
    ("idx_finding_alert_category","findings", "alert_category"),
]


def run():
    from database.connection import get_db_manager

    db = get_db_manager()
    db.initialize()
    if not db.health_check():
        logger.error("Database not reachable — is PostgreSQL running?")
        sys.exit(1)

    engine = db._engine
    with engine.connect() as conn:
        # Add columns
        for col_name, col_type in ADD_COLUMNS:
            result = conn.execute(
                __import__("sqlalchemy").text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='findings' AND column_name=:col"
                ),
                {"col": col_name},
            )
            if result.fetchone():
                logger.info("Column already exists, skipping: %s", col_name)
            else:
                conn.execute(
                    __import__("sqlalchemy").text(
                        f"ALTER TABLE findings ADD COLUMN {col_name} {col_type}"
                    )
                )
                logger.info("Added column: %s %s", col_name, col_type)

        # Add indexes
        for idx_name, table, col in ADD_INDEXES:
            result = conn.execute(
                __import__("sqlalchemy").text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename=:tbl AND indexname=:idx"
                ),
                {"tbl": table, "idx": idx_name},
            )
            if result.fetchone():
                logger.info("Index already exists, skipping: %s", idx_name)
            else:
                conn.execute(
                    __import__("sqlalchemy").text(
                        f"CREATE INDEX {idx_name} ON {table} ({col})"
                    )
                )
                logger.info("Created index: %s", idx_name)

        conn.commit()

    logger.info("Migration complete.")


if __name__ == "__main__":
    run()
