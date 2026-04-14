#!/usr/bin/env python3
"""
Initialize Vigil SOC database schema.

Calls ``init_database(create_tables=True)`` which runs
``Base.metadata.create_all()`` against the configured PostgreSQL database.
All ORM models are imported inside ``database/connection.py`` so every
``__tablename__`` is registered with ``Base.metadata`` before creation.

This script is idempotent (``CREATE TABLE IF NOT EXISTS`` semantics) and
is meant to run during startup before any user/role bootstrap script.

Exits non-zero on failure so the caller can fail loudly instead of
silently falling through to a JSON storage fallback.
"""

import logging
import sys
from pathlib import Path

# Ensure project root is on sys.path when invoked directly
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from database.connection import init_database, get_db_manager  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("init_schema")


def main() -> int:
    logger.info("Initializing database schema (create_all)...")
    try:
        init_database(echo=False, create_tables=True)
    except Exception as exc:
        logger.error("Failed to initialize database schema: %s", exc)
        return 1

    db_manager = get_db_manager()
    if not db_manager.health_check():
        logger.error("Database health check failed after schema init")
        return 1

    logger.info("Database schema ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
